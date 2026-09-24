"""Deluge Web UI JSON-RPC. The daemon must already be connected in the Web UI."""

import base64

import httpx

from app.adapters.contracts import (
    AdapterError,
    Capabilities,
    DownloadFile,
    FailureKind,
    SubmissionReceipt,
)
from app.adapters.http import configured_url
from app.adapters.qbittorrent import (
    MAX_ARTIFACT,
    QbitState,
    absolute_path,
    hash_value,
    integer,
    magnet_hashes,
    progress,
    relative_torrent_path,
    validate_attempt_tag,
)
from app.adapters.torrent_rpc import RpcTransport

FIELDS = [
    "hash",
    "save_path",
    "state",
    "progress",
    "total_size",
    "files",
    "file_progress",
    "file_priorities",
    "is_finished",
    "is_auto_managed",
    "move_completed",
    "label",
]


class DelugeClient(RpcTransport):
    name = "Deluge"

    def __init__(self, base_url, username="", password="", *, transport=None):
        url = configured_url(base_url).rstrip("/")
        if not url.endswith("/json"):
            url += "/json"
        self.rpc_url = url
        self.client = httpx.AsyncClient(
            base_url=url,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=10),
            transport=transport,
        )
        self.password, self.authenticated, self.counter = password, False, 0
        self._capabilities = None

    async def rpc(self, method, params=None, *, mutating=False):
        if not self.authenticated and method != "auth.login":
            if await self.rpc("auth.login", [self.password]) is not True:
                raise AdapterError(
                    FailureKind.AUTHENTICATION, "Deluge Web UI password was not accepted."
                )
            self.authenticated = True
        self.counter += 1
        result = await self.post(
            {"id": self.counter, "method": method, "params": params or []}, mutating=mutating
        )
        if result.get("id") != self.counter or result.get("error") or "result" not in result:
            raise AdapterError(
                FailureKind.UNCERTAIN if mutating else FailureKind.PARSER,
                "Deluge did not confirm the requested RPC operation.",
            )
        return result["result"]

    async def capabilities(self):
        if self._capabilities is None:
            if await self.rpc("web.connected") is not True:
                raise AdapterError(
                    FailureKind.UNAVAILABLE, "Connect the Deluge Web UI to its daemon first."
                )
            version = await self.rpc("daemon.info")
            plugins = await self.rpc("core.get_enabled_plugins")
            operations = {"submit", "find", "status", "files", "pause", "resume"}
            if "Label" in plugins:
                operations.add("categories")
            self._capabilities = Capabilities(
                version=str(version),
                protocols={"torrent"},
                operations=operations,
                limitations=[
                    "No independent attempt tags: Dewarr uses unique download folders and v1 "
                    "hashes.",
                    "In-client seeding rename and sequential/first-last piece preferences are "
                    "unavailable.",
                    "Categories require an existing Label plugin label.",
                    "Dewarr downloads directly into the completed folder; automatic moves are "
                    "disabled for these transfers.",
                    "Magnet metadata inspection requires a separate capable resolver.",
                ],
            )
        return self._capabilities

    async def label_options(self, category):
        caps = await self.capabilities()
        if not category:
            return {}
        if "categories" not in caps.operations or category not in await self.rpc(
            "label.get_labels"
        ):
            raise AdapterError(
                FailureKind.UNSUPPORTED,
                "Enable the Deluge Label plugin and create the selected label first.",
            )
        return await self.rpc("label.get_options", [category])

    async def download_location(self, category):
        options = await self.label_options(category)
        config = await self.rpc(
            "core.get_config_values",
            [["download_location", "move_completed", "move_completed_path"]],
        )
        if options.get("apply_move_completed"):
            moved, path = options.get("move_completed"), options.get("move_completed_path")
        else:
            moved, path = config.get("move_completed"), config.get("move_completed_path")
        return absolute_path(path if moved else config["download_location"])

    async def find(self, *, attempt_tag, torrent_hash=None):
        validate_attempt_tag(attempt_tag)
        if not torrent_hash or len(hash_value(torrent_hash)) != 40:
            return []
        fields = (
            FIELDS
            if "categories" in (await self.capabilities()).operations
            else [f for f in FIELDS if f != "label"]
        )
        rows = await self.rpc("core.get_torrents_status", [{"id": [torrent_hash]}, fields])
        if not isinstance(rows, dict):
            raise AdapterError(FailureKind.PARSER, "Deluge returned an invalid torrent list.")
        return [parse_state(key, row) for key, row in rows.items()]

    async def status(self, external_id):
        digest = hash_value(external_id)
        rows = await self.find(attempt_tag="book-search:observation", torrent_hash=digest)
        if len(rows) != 1:
            raise AdapterError(FailureKind.NOT_FOUND, "Deluge transfer was not found.")
        return rows[0]

    async def submit(self, content, *, attempt_tag, save_path, category):
        validate_attempt_tag(attempt_tag)
        await self.label_options(category)
        options = {
            "download_location": absolute_path(save_path),
            "add_paused": True,
            "auto_managed": False,
            "move_completed": False,
        }
        if isinstance(content, str):
            if any(len(digest) != 40 for digest in magnet_hashes(content)):
                raise AdapterError(
                    FailureKind.UNSUPPORTED, "Deluge requires a v1 torrent identity."
                )
            digest = await self.rpc("core.add_torrent_magnet", [content, options], mutating=True)
        else:
            if not content or len(content) > MAX_ARTIFACT:
                raise ValueError("Invalid torrent size")
            digest = await self.rpc(
                "core.add_torrent_file",
                ["dewarr.torrent", base64.b64encode(content).decode(), options],
                mutating=True,
            )
        try:
            digest = hash_value(digest)
        except (ValueError, TypeError) as exc:
            raise AdapterError(
                FailureKind.UNCERTAIN, "Deluge add was not confirmed; reconcile before continuing."
            ) from exc
        # A duplicate can appear between preflight and add. The acknowledgement
        # alone never authorizes labeling or resuming somebody else's transfer.
        observed = await self.status(digest)
        if observed.save_path != absolute_path(save_path) or digest not in observed.identities:
            raise AdapterError(
                FailureKind.UNCERTAIN,
                "Deluge returned a transfer outside the recorded folder; it was not changed.",
            )
        if category:
            await self.rpc("label.set_torrent", [digest, category], mutating=True)
        # A label may apply daemon defaults. Freeze this new transfer's location;
        # no move_storage, rename, remove, recheck or daemon settings writes are used.
        await self.rpc(
            "core.set_torrent_options",
            [[digest], {"move_completed": False, "auto_managed": False}],
            mutating=True,
        )
        await self.resume(digest)
        return SubmissionReceipt(external_ids=[digest])

    async def pause(self, external_id):
        await self.rpc("core.pause_torrent", [[hash_value(external_id)]], mutating=True)

    async def resume(self, external_id):
        await self.rpc("core.resume_torrent", [[hash_value(external_id)]], mutating=True)


def parse_state(digest, row):
    try:
        digest = hash_value(digest)
        if len(digest) != 40 or row["hash"].lower() != digest:
            raise ValueError("Inconsistent identity")
        files, amounts, priorities = row["files"], row["file_progress"], row["file_priorities"]
        if not files or not len(files) == len(amounts) == len(priorities):
            raise ValueError("Inconsistent files")
        parsed = [
            DownloadFile(
                relative_path=relative_torrent_path(f["path"]),
                size_bytes=integer(f["size"]),
                complete=progress(p) == 1,
            )
            for f, p in zip(files, amounts, strict=True)
        ]
        if len({f.relative_path for f in parsed}) != len(parsed):
            raise ValueError("Duplicate paths")
        selected = all(integer(p) > 0 for p in priorities)
        total = integer(row["total_size"])
        done = progress(row["progress"] / 100)
        complete = (
            row["state"] in {"Seeding", "Paused", "Queued"}
            and row["is_finished"] is True
            and done == 1
            and selected
            and all(f.complete for f in parsed)
        )
        managed = row["is_auto_managed"] or row["move_completed"]
        if type(managed) is not bool:
            raise ValueError("Invalid move settings")
        return QbitState(
            external_id=digest,
            infohash_v1=digest,
            tags=set(),
            category=row.get("label", ""),
            auto_managed=managed,
            save_path=absolute_path(row["save_path"]),
            state="failed" if row["state"] == "Error" else row["state"],
            progress=done,
            total_bytes=total,
            files=parsed,
            all_files_selected=selected,
            reported_complete=complete,
            completed=complete and sum(f.size_bytes for f in parsed) == total,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise AdapterError(
            FailureKind.PARSER, "Deluge returned inconsistent transfer evidence."
        ) from exc
