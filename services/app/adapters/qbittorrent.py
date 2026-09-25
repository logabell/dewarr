"""qBittorrent 5.x transport and observation contract.

Submission is not association or completion. Callers must journal dispatch before
calling submit, then reconcile identity, tag and destination before importing.
Observation does not change a torrent. rename_file and set_location are the
explicit ways to move or rename a seeding copy.
"""

import asyncio
import base64
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import Field

from app.adapters.contracts import (
    AdapterError,
    Capabilities,
    DownloadFile,
    DownloadState,
    FailureKind,
    SubmissionReceipt,
)
from app.adapters.http import configured_url

MAX_RESPONSE = 16 * 1024 * 1024
MAX_ARTIFACT = 16 * 1024 * 1024
READY_STATES = {"uploading", "stalledUP", "queuedUP", "stoppedUP", "forcedUP"}


def hash_value(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise ValueError("Expected a torrent info hash")
    return value.lower()


def validate_attempt_tag(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"book-search:[a-zA-Z0-9_-]{1,80}", value):
        raise ValueError("Expected an application attempt tag")
    return value


def absolute_path(value: str) -> str:
    # The supported deployment contract is a POSIX qBit container. A Windows
    # backend needs an explicit path dialect, not host-dependent normalization.
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or value != value.strip()
        or any(p in {".", "..", ""} for p in value.rstrip("/")[1:].split("/"))
    ):
        raise ValueError("Expected an absolute POSIX download path")
    return value.rstrip("/")


def _windows_drive_prefix(part: str) -> bool:
    """A colon inside a title is a normal POSIX name. C: and C:foo are not."""
    return (
        len(part) >= 2
        and part[0].isalpha()
        and part[1] == ":"
        and (len(part) == 2 or part[2] != " ")
    )


def unsafe_relative_path(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in {".", "..", ""} for part in value.split("/"))
        or any(_windows_drive_prefix(part) for part in value.split("/"))
    ):
        return True
    return False


def relative_torrent_path(value: str) -> str:
    if unsafe_relative_path(value):
        raise ValueError("Unsafe torrent path")
    return value


def magnet_hashes(value: str) -> set[str]:
    """Accept one magnet; remote .torrent URLs must be resolved by source adapters."""
    if (
        len(value) > 32768
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
        or not value.startswith("magnet:?")
    ):
        raise ValueError("Expected one magnet URI")
    parts = urlsplit(value)
    if parts.netloc or parts.fragment:
        raise ValueError("Invalid magnet URI")
    identities = set()
    by_kind = {}
    for xt in parse_qs(parts.query, max_num_fields=100).get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            digest = xt[9:]
            if re.fullmatch(r"[a-zA-Z2-7]{32}", digest):
                digest = base64.b32decode(digest.upper()).hex()
            if not re.fullmatch(r"[a-fA-F0-9]{40}", digest):
                raise ValueError("Invalid v1 magnet identity")
            kind = "v1"
        elif xt.lower().startswith("urn:btmh:1220"):
            digest = xt[13:]
            if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                raise ValueError("Invalid v2 magnet identity")
            kind = "v2"
        else:
            raise ValueError("Unsupported magnet identity")
        digest = digest.lower()
        if kind in by_kind and by_kind[kind] != digest:
            raise ValueError("Conflicting magnet identities")
        by_kind[kind] = digest
        identities.add(digest)
    if not identities:
        raise ValueError("Magnet has no supported torrent identity")
    return identities


def optional_metric(value, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value if maximum is None or value < maximum else None


class QbitState(DownloadState):
    infohash_v1: str | None = None
    infohash_v2: str | None = None
    tags: set[str] = Field(default_factory=set)
    category: str
    auto_managed: bool
    progress: float
    total_bytes: int
    download_speed: int | None = Field(default=None, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    all_files_selected: bool
    reported_complete: bool = False
    seeders: int | None = Field(default=None, ge=0)

    @property
    def identities(self) -> set[str]:
        # The qBit API key is not necessarily a v1 hash. In particular v2 uses
        # a truncated key; only properties provide the full protocol identity.
        return {value for value in (self.infohash_v1, self.infohash_v2) if value}


def verify_association(
    states: list[QbitState], *, tag: str, hashes: set[str], save_path: str, category: str
) -> QbitState | None:
    """None means not observed, never permission to retry an uncertain submission."""
    tag, save_path = validate_attempt_tag(tag), absolute_path(save_path)
    hashes = {hash_value(value) for value in hashes}
    if not hashes:
        raise ValueError("Association requires artifact identity")
    if not states:
        return None
    if len(states) != 1:
        raise AdapterError(FailureKind.UNCERTAIN, "More than one transfer matches this attempt.")
    state = states[0]
    if (
        tag not in state.tags
        or not hashes.issubset(state.identities)
        or state.save_path != save_path
        or state.category != category
    ):
        raise AdapterError(
            FailureKind.UNCERTAIN,
            "The existing transfer does not match the recorded attempt and destination.",
        )
    return state.model_copy(update={"association_verified": True})


def integer(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("Invalid integer")
    return value


def progress(value):
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Invalid progress")
    return float(value)


def parse_state(row: dict, properties: dict, files: list) -> QbitState:
    try:
        key = hash_value(row["hash"])
        hashes = []
        for field, length in (("infohash_v1", 40), ("infohash_v2", 64)):
            value = properties[field]
            if value:
                value = hash_value(value)
                if len(value) != length:
                    raise ValueError("Incorrect hash type")
            elif value != "":
                raise ValueError("Invalid missing hash")
            hashes.append(value or None)
        if not any(hashes) or key not in {h[:40] for h in hashes if h}:
            raise ValueError("Inconsistent API key and identity")
        path = absolute_path(row["save_path"])
        if absolute_path(properties["save_path"]) != path:
            raise ValueError("Download moved during observation")
        if not isinstance(row["tags"], str) or not isinstance(row["category"], str):
            raise ValueError("Missing association fields")
        if type(row["auto_tmm"]) is not bool or not isinstance(row["state"], str):
            raise ValueError("Invalid state")
        amount_left, total = integer(row["amount_left"]), integer(row["total_size"])
        completed = progress(row["progress"])
        if not isinstance(files, list) or len(files) > 100000:
            raise ValueError("Invalid file list")
        parsed, names, indexes = [], set(), set()
        all_selected = bool(files)
        for file in files:
            name = file["name"]
            if unsafe_relative_path(name):
                raise ValueError("Unsafe file path")
            index = integer(file["index"])
            if name in names or index in indexes:
                raise ValueError("Duplicate file")
            names.add(name)
            indexes.add(index)
            priority = integer(file["priority"])
            if priority not in {0, 1, 6, 7}:
                raise ValueError("Unknown file priority")
            all_selected &= priority != 0
            parsed.append(
                DownloadFile(
                    relative_path=name,
                    size_bytes=integer(file["size"]),
                    complete=progress(file["progress"]) == 1,
                )
            )
        reported_complete = (
            row["state"] in READY_STATES
            and completed == 1
            and amount_left == 0
            and total > 0
            and all_selected
            and all(f.complete for f in parsed)
        )
        return QbitState(
            reported_complete=reported_complete,
            external_id=key,
            state=row["state"],
            completed=(reported_complete and sum(f.size_bytes for f in parsed) == total),
            save_path=path,
            files=parsed,
            infohash_v1=hashes[0],
            infohash_v2=hashes[1],
            tags={tag.strip() for tag in row["tags"].split(",") if tag.strip()},
            category=row["category"],
            auto_managed=row["auto_tmm"],
            progress=completed,
            download_speed=optional_metric(row.get("dlspeed")),
            eta_seconds=optional_metric(row.get("eta"), maximum=8640000),
            seeders=integer(row["num_seeds"]) if "num_seeds" in row else None,
            total_bytes=total,
            all_files_selected=all_selected,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AdapterError(
            FailureKind.PARSER, "qBittorrent returned incomplete or inconsistent transfer evidence."
        ) from error


class QbitClient:
    def __init__(self, base_url: str, username: str, password: str, *, transport=None):
        endpoint = configured_url(base_url)
        origin = urlsplit(endpoint)
        self._username, self._password = username, password
        self._authenticated = False
        self._capabilities = None
        self.client = httpx.AsyncClient(
            base_url=endpoint + "/api/v2/",
            headers={
                "Origin": f"{origin.scheme}://{origin.netloc}",
                "Referer": endpoint + "/",
                "User-Agent": "BookSearch/0.1",
            },
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30, connect=10),
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def _request(self, method, path, *, mutating=False, accepted_statuses=(), **kwargs):
        try:
            async with (
                asyncio.timeout(45),
                self.client.stream(method, path, **kwargs) as response,
            ):
                status = response.status_code
                if status in {401, 403}:
                    self._authenticated = False
                    raise AdapterError(
                        FailureKind.AUTHENTICATION,
                        "qBittorrent rejected authentication or access. Check the connection.",
                    )
                if mutating and status in {400, 415}:
                    rejected = (
                        "qBittorrent rejected the rename."
                        if path.startswith("torrents/rename") or path == "torrents/setLocation"
                        else "qBittorrent rejected the torrent input."
                    )
                    raise AdapterError(FailureKind.PARSER, rejected)
                if 300 <= status < 400:
                    raise AdapterError(
                        FailureKind.UNCERTAIN if mutating else FailureKind.ROUTE,
                        "qBittorrent redirected the operation. Check its final URL and reconcile.",
                    )
                if status == 404 and not mutating:
                    raise AdapterError(FailureKind.NOT_FOUND, "qBittorrent resource was not found.")
                accepted = {200, 202} if mutating else {200}
                accepted.update(accepted_statuses)
                if path == "auth/login":
                    accepted.add(204)
                if status not in accepted:
                    raise AdapterError(
                        FailureKind.UNCERTAIN if mutating else FailureKind.UNAVAILABLE,
                        "Submission was not confirmed. Reconcile before submitting again."
                        if mutating
                        else "qBittorrent could not complete the request.",
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise AdapterError(
                            FailureKind.UNCERTAIN if mutating else FailureKind.PARSER,
                            "qBittorrent response exceeded the size limit.",
                        )
                return status, bytes(content)
        except (httpx.HTTPError, TimeoutError) as error:
            raise AdapterError(
                FailureKind.UNCERTAIN if mutating else FailureKind.ROUTE,
                "qBittorrent submission outcome is unknown. Reconcile before submitting again."
                if mutating
                else "qBittorrent could not be reached.",
            ) from error

    async def _login(self):
        if self._authenticated:
            return
        if not self._username and not self._password:
            self._authenticated = True
            return
        status, result = await self._request(
            "POST", "auth/login", data={"username": self._username, "password": self._password}
        )
        if not (status == 204 and not result) and result.strip() != b"Ok.":
            raise AdapterError(FailureKind.AUTHENTICATION, "qBittorrent login was not accepted.")
        self._authenticated = True

    async def capabilities(self) -> Capabilities:
        if self._capabilities:
            return self._capabilities
        await self._login()
        _, raw = await self._request("GET", "app/version")
        if not re.fullmatch(rb"v?5\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)?", raw.strip()):
            raise AdapterError(FailureKind.UNSUPPORTED, "This adapter requires qBittorrent 5.x.")
        _, api = await self._request("GET", "app/webapiVersion")
        if not re.fullmatch(rb"2\.\d+\.\d+", api.strip()):
            raise AdapterError(FailureKind.UNSUPPORTED, "Unsupported qBittorrent Web API.")
        self._capabilities = Capabilities(
            version=raw.strip().decode(),
            operations={"submit", "find", "status", "files"},
            protocols={"torrent"},
            limitations=["POSIX paths; actual server compatibility requires certification."],
        )
        version = tuple(map(int, raw.strip().decode().lstrip("v").split(".")[:2]))
        if version >= (5, 2):
            self._capabilities.operations.add("magnet-metadata")
        return self._capabilities

    async def download_location(self, category: str) -> str:
        """Read the server's destination without changing any qBittorrent settings."""
        await self._login()
        _, raw = await self._request("GET", "app/preferences")
        _, categories_raw = await self._request("GET", "torrents/categories")
        try:
            preferences = json.loads(raw)
            categories = json.loads(categories_raw)
            path = absolute_path(preferences["save_path"])
            if preferences.get("auto_tmm_enabled") or preferences.get(
                "use_category_paths_in_manual_mode"
            ):
                category_path = categories.get(category, {}).get("savePath") or category
                if category_path:
                    path = absolute_path(
                        category_path
                        if category_path.startswith("/")
                        else path.rstrip("/") + "/" + category_path
                    )
            return path
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise AdapterError(
                FailureKind.PARSER, "qBittorrent returned an invalid download location."
            ) from error

    async def resolve_magnet(self, magnet: str, *, wait_seconds=40, interval=2) -> bytes:
        """Fetch metadata through qBit's network without adding or starting a transfer.

        This uses the 5.2 metadata cache API. Never emulate it by adding a magnet
        and racing to stop a payload download, or by running a local P2P session.
        The exported torrent, not the JSON/page file claims, establishes identity.
        """
        expected = magnet_hashes(magnet)
        # Do not let a source-controlled URL turn metadata inspection into an
        # arbitrary web fetch through the downloader's trusted network.
        if set(parse_qs(urlsplit(magnet).query)) - {"xt", "tr", "dn"}:
            raise AdapterError(
                FailureKind.UNSUPPORTED,
                "Magnet inspection supports torrent identities and trackers only.",
            )
        capabilities = await self.capabilities()
        if "magnet-metadata" not in capabilities.operations:
            raise AdapterError(
                FailureKind.UNSUPPORTED,
                "Magnet inspection requires qBittorrent 5.2 or newer with metadata cache APIs.",
            )
        from app.adapters.torrent_descriptor import inspect_torrent

        try:
            async with asyncio.timeout(wait_seconds):
                while True:
                    status, raw = await self._request(
                        "POST",
                        "torrents/fetchMetadata",
                        data={"source": magnet},
                        accepted_statuses=(202,),
                    )
                    try:
                        observation = json.loads(raw)
                        if not isinstance(observation, dict):
                            raise ValueError("Invalid metadata observation")
                    except (ValueError, UnicodeError) as error:
                        raise AdapterError(
                            FailureKind.PARSER, "qBittorrent returned unreadable metadata status."
                        ) from error
                    if status == 202:
                        await asyncio.sleep(interval)
                        continue
                    if not isinstance(observation.get("info"), dict):
                        raise AdapterError(
                            FailureKind.PARSER,
                            "qBittorrent did not establish resolved torrent metadata.",
                        )
                    status, raw = await self._request(
                        "POST",
                        "torrents/saveMetadata",
                        data={"source": magnet},
                        accepted_statuses=(409,),
                    )
                    if status == 409:
                        # fetchMetadata may have observed an existing transfer,
                        # which is not necessarily present in its separate cache.
                        # Exporting it is read-only and never grants adoption.
                        try:
                            key = hash_value(observation["hash"])
                        except (KeyError, ValueError) as error:
                            raise AdapterError(
                                FailureKind.PARSER,
                                "qBittorrent returned an invalid metadata identity.",
                            ) from error
                        _, raw = await self._request("GET", "torrents/export", params={"hash": key})
                    descriptor = await inspect_torrent(raw)
                    hashes = {h for h in (descriptor.infohash_v1, descriptor.infohash_v2) if h}
                    if not expected <= hashes:
                        raise AdapterError(
                            FailureKind.PARSER,
                            "Resolved torrent does not match the requested magnet identity.",
                        )
                    return raw
        except TimeoutError as error:
            raise AdapterError(
                FailureKind.TIMEOUT,
                "Torrent metadata is not available yet; retry inspection later.",
            ) from error

    async def _json(self, path, **params):
        await self._login()
        _, raw = await self._request("GET", path, params=params)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise AdapterError(
                FailureKind.PARSER, "qBittorrent returned unreadable data."
            ) from error

    async def _rows(self, **params):
        value = await self._json("torrents/info", limit=3, **params)
        if (
            not isinstance(value, list)
            or len(value) > 2
            or any(not isinstance(row, dict) for row in value)
        ):
            raise AdapterError(FailureKind.PARSER, "qBittorrent returned an invalid match set.")
        return value

    async def _observe(self, row):
        try:
            key = hash_value(row["hash"])
        except (KeyError, ValueError) as error:
            raise AdapterError(
                FailureKind.PARSER, "qBittorrent returned an invalid key."
            ) from error
        properties = await self._json("torrents/properties", hash=key)
        files = await self._json("torrents/files", hash=key)
        return parse_state(row, properties, files)

    async def find(self, *, attempt_tag: str, torrent_hash: str | None) -> list[QbitState]:
        tag = validate_attempt_tag(attempt_tag)
        digest = hash_value(torrent_hash) if torrent_hash else None
        # Query separately: combining filters would hide a hash collision with an
        # unrelated untagged transfer. Full v2 identity is checked via properties.
        rows = await self._rows(tag=tag)
        if digest:
            rows += await self._rows(hashes=digest[:40])
        result = {}
        for row in rows:
            if isinstance(row.get("hash"), str) and row["hash"].lower() in result:
                continue
            state = await self._observe(row)
            if tag in state.tags or (digest and digest in state.identities):
                result[state.external_id] = state
            else:
                raise AdapterError(FailureKind.PARSER, "qBittorrent returned an unrelated match.")
        return list(result.values())

    async def status(self, external_id: str) -> QbitState:
        key = hash_value(external_id)
        rows = await self._rows(hashes=key)
        if not rows:
            raise AdapterError(FailureKind.NOT_FOUND, "The recorded transfer is no longer visible.")
        if len(rows) != 1:
            raise AdapterError(
                FailureKind.PARSER, "qBittorrent returned multiple transfer records."
            )
        state = await self._observe(rows[0])
        if state.external_id != key:
            raise AdapterError(FailureKind.PARSER, "qBittorrent returned a different transfer.")
        return state

    async def _mutate(self, path, data):
        await self.capabilities()
        await self._login()
        status, result = await self._request(
            "POST", path, data=data, mutating=True, accepted_statuses=(409,)
        )
        if status == 409:
            return False
        if result.strip() not in {b"", b"Ok."}:
            raise AdapterError(
                FailureKind.PARSER, "qBittorrent returned an unexpected rename result."
            )
        return True

    async def cleanup_transfer(self, torrent_hash: str, *, remove: bool) -> bool:
        """Remove only the client record; deleting content is deliberately impossible."""
        return await self._mutate(
            "torrents/delete" if remove else "torrents/stop",
            {"hashes": hash_value(torrent_hash), **({"deleteFiles": "false"} if remove else {})},
        )

    async def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> bool:
        """Rename one torrent file. False means qBittorrent refused the change."""
        return await self._mutate(
            "torrents/renameFile",
            {
                "hash": hash_value(torrent_hash),
                "oldPath": relative_torrent_path(old_path),
                "newPath": relative_torrent_path(new_path),
            },
        )

    async def set_location(self, torrent_hash: str, location: str) -> bool:
        """Move torrent content to an absolute directory. False means it was refused."""
        return await self._mutate(
            "torrents/setLocation",
            {"hashes": hash_value(torrent_hash), "location": absolute_path(location)},
        )

    async def directory_entries(self, path: str) -> list[str]:
        """List subdirectory names qBittorrent sees at an absolute path."""
        await self.capabilities()
        await self._login()
        _status, result = await self._request(
            "POST",
            "app/getDirectoryContent",
            data={"dirPath": absolute_path(path)},
        )
        try:
            entries = json.loads(result)
        except json.JSONDecodeError as error:
            raise AdapterError(
                FailureKind.PARSER, "qBittorrent returned an unreadable folder listing."
            ) from error
        if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
            raise AdapterError(
                FailureKind.PARSER, "qBittorrent returned an unreadable folder listing."
            )
        return entries

    async def census(self, *, known_hashes: set[str], categories: set[str], pulse):
        """Enumerate the whole client, then observe application-relevant transfers.

        Identity/routing markers are checked again after file observations. Progress and
        speeds may advance normally. No remote transfer is adopted or changed here.
        """
        keys = {hash_value(value)[:40] for value in known_hashes}
        fields = (
            "hash",
            "tags",
            "category",
            "save_path",
            "auto_tmm",
            "added_on",
            "name",
            "total_size",
        )

        async def listing():
            result, offset, previous = {}, 0, ""
            while True:
                await pulse()
                rows = await self._json("torrents/info", sort="hash", limit=250, offset=offset)
                if not isinstance(rows, list) or len(rows) > 250:
                    raise AdapterError(FailureKind.PARSER, "Invalid downloader census page")
                if not rows:
                    return result
                for row in rows:
                    try:
                        key = hash_value(row["hash"])
                        if key <= previous or any(field not in row for field in fields):
                            raise ValueError
                        if not isinstance(row["tags"], str) or not isinstance(row["category"], str):
                            raise ValueError
                        result[key] = {field: row[field] for field in fields}
                        previous = key
                    except (KeyError, TypeError, ValueError):
                        raise AdapterError(
                            FailureKind.PARSER,
                            "Downloader census repeated or omitted identity evidence",
                        ) from None
                    if len(result) > 10000:
                        raise AdapterError(
                            FailureKind.UNSUPPORTED,
                            "Recovery census supports at most 10,000 transfers per downloader",
                        )
                offset += len(rows)

        await self.capabilities()
        first = await listing()
        if first != await listing():
            raise AdapterError(
                FailureKind.UNCERTAIN, "Downloader membership or routing changed during census"
            )
        states = []
        for key, row in first.items():
            tags = {tag.strip() for tag in row["tags"].split(",") if tag.strip()}
            if (
                key in keys
                or row["category"] in categories
                or any(tag.startswith("book-search:") for tag in tags)
            ):
                await pulse()
                state = await self.status(key)
                if (
                    state.tags != tags
                    or state.category != row["category"]
                    or state.save_path != absolute_path(row["save_path"])
                    or state.auto_managed != row["auto_tmm"]
                ):
                    raise AdapterError(
                        FailureKind.UNCERTAIN, "Downloader routing changed while reading a transfer"
                    )
                states.append(state)
        if first != await listing():
            raise AdapterError(
                FailureKind.UNCERTAIN, "Downloader changed after transfer observations"
            )
        return states, len(first)

    async def submit(
        self,
        artifact: bytes | str,
        *,
        attempt_tag: str,
        save_path: str,
        category: str = "",
    ) -> SubmissionReceipt:
        tag = validate_attempt_tag(attempt_tag)
        absolute_path(save_path)  # Observed destination is used for later reconciliation.
        if not re.fullmatch(r"[A-Za-z0-9_-]{0,100}", category):
            raise ValueError("Use one simple download category")
        fields = {
            "tags": tag,
            "category": category,
        }
        kwargs = {"data": fields}
        if isinstance(artifact, str):
            magnet_hashes(artifact)
            fields["urls"] = artifact
        elif isinstance(artifact, bytes) and 0 < len(artifact) <= MAX_ARTIFACT:
            kwargs["files"] = {"torrents": ("book.torrent", artifact, "application/x-bittorrent")}
        else:
            raise ValueError("Expected one bounded torrent artifact")
        await self.capabilities()
        await self._login()
        status, result = await self._request("POST", "torrents/add", mutating=True, **kwargs)
        if status == 200 and result.strip() == b"Ok.":
            return SubmissionReceipt()
        # 5.2.3 uses a structured acknowledgement. Earlier 5.x returns Ok.
        # Neither form establishes ownership or a confirmed client association.
        try:
            receipt = json.loads(result)
            success = integer(receipt["success_count"])
            pending = integer(receipt["pending_count"])
            failed = integer(receipt["failure_count"])
            ids = receipt["added_torrent_ids"]
            if (
                failed != 0
                or success + pending != 1
                or not isinstance(ids, list)
                or len(ids) != success
                or any(len(hash_value(key)) != 40 for key in ids)
                or (pending == 1) != (status == 202)
            ):
                raise ValueError("Unexpected single-artifact receipt")
            return SubmissionReceipt(
                external_ids=[key.lower() for key in ids], pending=bool(pending)
            )
        except (ValueError, KeyError, TypeError) as error:
            raise AdapterError(
                FailureKind.UNCERTAIN,
                "qBittorrent did not confirm submission. Reconcile before submitting again.",
            ) from error
