"""Transmission 3/4 RPC. Only the explicit session-id rejection can retry a write."""

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
    "hashString",
    "downloadDir",
    "labels",
    "status",
    "percentDone",
    "totalSize",
    "leftUntilDone",
    "files",
    "fileStats",
    "error",
]


class TransmissionClient(RpcTransport):
    name = "Transmission"

    def __init__(self, base_url, username="", password="", *, transport=None):
        url = configured_url(base_url).rstrip("/")
        if not url.endswith("/rpc"):
            url += "/rpc" if url.endswith("/transmission") else "/transmission/rpc"
        self.rpc_url = url
        self.client = httpx.AsyncClient(
            base_url=url,
            auth=(username, password) if username or password else None,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=10),
            transport=transport,
        )
        self._capabilities = None

    async def rpc(self, method, arguments=None, *, mutating=False):
        payload = {"method": method, "arguments": arguments or {}}
        for _ in range(2):
            result = await self.post(payload, mutating=mutating, handshake=True)
            if result is None:
                continue
            if result.get("result") != "success" or not isinstance(result.get("arguments"), dict):
                raise AdapterError(
                    FailureKind.UNCERTAIN if mutating else FailureKind.PARSER,
                    "Transmission did not confirm the requested operation.",
                )
            return result["arguments"]
        raise AdapterError(
            FailureKind.AUTHENTICATION, "Transmission session handshake was not accepted."
        )

    async def capabilities(self):
        if self._capabilities is None:
            value = await self.rpc("session-get")
            if type(value.get("rpc-version")) is not int or value["rpc-version"] < 16:
                raise AdapterError(
                    FailureKind.UNSUPPORTED, "Transmission 3.0 or newer with labels is required."
                )
            self._capabilities = Capabilities(
                version=str(value.get("version", "")),
                protocols={"torrent"},
                operations={
                    "submit",
                    "find",
                    "status",
                    "files",
                    "attempt-tagging",
                    "categories",
                    "pause",
                    "resume",
                },
                limitations=[
                    "Only v1 torrent identities are supported.",
                    "In-client seeding rename and sequential/first-last piece preferences are "
                    "unavailable.",
                    "Magnet metadata inspection requires a separate capable resolver.",
                ],
            )
        return self._capabilities

    async def download_location(self, category):
        return absolute_path((await self.rpc("session-get"))["download-dir"])

    async def find(self, *, attempt_tag, torrent_hash=None):
        validate_attempt_tag(attempt_tag)
        args = {"fields": FIELDS}
        # Include matching labels as well as hashes to detect inconsistent evidence.
        rows = (await self.rpc("torrent-get", args)).get("torrents")
        if not isinstance(rows, list):
            raise AdapterError(FailureKind.PARSER, "Transmission returned an invalid torrent list.")
        return [
            parse_state(row)
            for row in rows
            if attempt_tag in row.get("labels", [])
            or row.get("hashString", "").lower() == torrent_hash
        ]

    async def status(self, external_id):
        rows = (
            await self.rpc("torrent-get", {"fields": FIELDS, "ids": [hash_value(external_id)]})
        ).get("torrents", [])
        if len(rows) != 1:
            raise AdapterError(FailureKind.NOT_FOUND, "Transmission transfer was not found.")
        return parse_state(rows[0])

    async def submit(self, content, *, attempt_tag, save_path, category):
        await self.capabilities()
        args = {
            "download-dir": absolute_path(save_path),
            "paused": False,
            "labels": [validate_attempt_tag(attempt_tag)] + ([category] if category else []),
        }
        if isinstance(content, str):
            if any(len(digest) != 40 for digest in magnet_hashes(content)):
                raise AdapterError(
                    FailureKind.UNSUPPORTED, "Transmission requires a v1 torrent identity."
                )
            args["filename"] = content
        else:
            if not content or len(content) > MAX_ARTIFACT:
                raise ValueError("Invalid torrent size")
            args["metainfo"] = base64.b64encode(content).decode()
        result = await self.rpc("torrent-add", args, mutating=True)
        if "torrent-duplicate" in result:
            raise AdapterError(
                FailureKind.UNCERTAIN,
                "Transmission reported a pre-existing torrent; it was not adopted.",
            )
        try:
            digest = hash_value(result["torrent-added"]["hashString"])
        except (KeyError, ValueError, TypeError) as exc:
            raise AdapterError(
                FailureKind.UNCERTAIN, "Transmission add was not confirmed."
            ) from exc
        return SubmissionReceipt(external_ids=[digest])

    async def pause(self, external_id):
        await self.rpc("torrent-stop", {"ids": [hash_value(external_id)]}, mutating=True)

    async def resume(self, external_id):
        await self.rpc("torrent-start", {"ids": [hash_value(external_id)]}, mutating=True)


def parse_state(row):
    try:
        digest = hash_value(row["hashString"])
        if len(digest) != 40:
            raise ValueError("Unsupported hash")
        labels = row["labels"]
        if not isinstance(labels, list) or any(not isinstance(v, str) for v in labels):
            raise ValueError("Invalid labels")
        files, stats = row["files"], row["fileStats"]
        if not isinstance(files, list) or not files or len(files) != len(stats):
            raise ValueError("Invalid files")
        parsed = [
            DownloadFile(
                relative_path=relative_torrent_path(f["name"]),
                size_bytes=integer(f["length"]),
                complete=integer(f["bytesCompleted"]) == f["length"],
            )
            for f in files
        ]
        if len({f.relative_path for f in parsed}) != len(parsed):
            raise ValueError("Duplicate paths")
        selected = all(s["wanted"] in (True, 1) for s in stats)
        total, done = integer(row["totalSize"]), progress(row["percentDone"])
        complete = (
            row["status"] in {0, 5, 6}
            and not row["error"]
            and done == 1
            and integer(row["leftUntilDone"]) == 0
            and selected
            and all(f.complete for f in parsed)
        )
        categories = [label for label in labels if not label.startswith("book-search:")]
        return QbitState(
            external_id=digest,
            infohash_v1=digest,
            tags=set(labels),
            category=categories[0]
            if len(categories) == 1
            else ""
            if not categories
            else "ambiguous",
            auto_managed=False,
            save_path=absolute_path(row["downloadDir"]),
            state="failed" if row["error"] else str(row["status"]),
            progress=done,
            total_bytes=total,
            files=parsed,
            all_files_selected=selected,
            reported_complete=complete,
            completed=complete and sum(f.size_bytes for f in parsed) == total,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise AdapterError(
            FailureKind.PARSER, "Transmission returned inconsistent transfer evidence."
        ) from exc
