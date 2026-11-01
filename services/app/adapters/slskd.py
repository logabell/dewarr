"""Soulseek source and downloader through one slskd process.

A result is one online user's folder, not a torrent. The same connection
searches and queues that file list as a download batch. slskd 0.26.0 reads
searchTimeout as milliseconds even though the request model says seconds
(https://github.com/slskd/slskd/issues/1820), so this client sends 15000.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, Field

from app.adapters.contracts import (
    AdapterError,
    DownloadFile,
    DownloadState,
    FailureKind,
    Release,
)
from app.adapters.http import configured_url
from app.adapters.torrent_descriptor import TorrentDescriptor, TorrentFile

SEARCH_TIMEOUT_MS = 15000
SEARCH_BUDGET_SECONDS = 20
SEARCH_CALL_TIMEOUT = 30
AUDIO = {".m4b", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".wma"}
EBOOK = {".epub", ".pdf", ".cbz", ".mobi", ".azw", ".azw3"}
MEDIA = AUDIO | EBOOK
COMPLETE = {"succeeded", "completed"}
FAILED = {"errored", "cancelled", "canceled", "timedout", "rejected", "aborted"}


class SlskdFile(BaseModel):
    filename: str
    size: int = Field(ge=0)


class SlskdRelease(Release):
    source: Literal["slskd"] = "slskd"
    title: str
    observed_at: datetime
    protocol: Literal["soulseek"] = "soulseek"
    username: str
    directory: str
    files: list[SlskdFile]
    peer_online: bool = True
    queue_length: int | None = Field(default=None, ge=0)
    upload_speed: int | None = Field(default=None, ge=0)
    free_upload_slot: bool | None = None
    search_id: str
    locked_files: int = Field(default=0, ge=0)


class SlskdState(DownloadState):
    reported_complete: bool = False
    total_bytes: int | None = Field(default=None, ge=0)


def version_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("full", "current", "version"):
            if isinstance(value.get(key), str):
                return value[key]
    return str(value)


def soulseek_connected(application: Any) -> bool:
    if not isinstance(application, dict):
        return False
    server = application.get("server")
    if isinstance(server, dict):
        state = server.get("state") or server.get("connectionState") or ""
    else:
        state = server or application.get("state") or ""
    text = str(state).lower()
    return "connected" in text and "disconnected" not in text


def extension(name: str) -> str:
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1]
    return leaf[leaf.rfind(".") :].lower() if "." in leaf else ""


def leaf_name(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def shared_directory(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""


def _contains_phrase(phrase: str, text: str) -> bool:
    needle = phrase.strip()
    if len(needle) < 2:
        return False
    return (
        re.search(rf"(?<![\w']){re.escape(needle)}(?![\w'])", text, flags=re.IGNORECASE) is not None
    )


def _matched(expected: list[str], text: str) -> list[str]:
    return [item for item in expected if _contains_phrase(item, text)]


def optional_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def folder_medium(files: list[SlskdFile]) -> str | None:
    kinds = set()
    for item in files:
        ext = extension(item.filename)
        if ext in AUDIO:
            kinds.add("audio")
        elif ext in EBOOK:
            kinds.add("ebook")
    if len(kinds) == 1:
        return kinds.pop()
    return None


def group_responses(
    responses: list[dict[str, Any]],
    *,
    search_id: str,
    observed_at: datetime,
    title: str,
    authors: list[str],
) -> list[SlskdRelease]:
    """Turn peer file lists into one release per shared folder."""
    grouped: dict[tuple[str, str], list[SlskdFile]] = {}
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    for response in responses[:200]:
        username = str(response.get("username") or "").strip()
        if not username:
            continue
        raw_files = list(response.get("files") or [])
        # A response longer than the cap can cut a folder in half. Leave that folder out.
        incomplete = {
            shared_directory(str(item.get("filename") or ""))
            for item in raw_files[500:]
            if extension(str(item.get("filename") or "")) in MEDIA
            and optional_count(item.get("size")) is not None
        }
        locked_dirs = {
            shared_directory(str(item.get("filename") or ""))
            for item in (response.get("lockedFiles") or [])
            if extension(str(item.get("filename") or "")) in MEDIA
        }
        for item in raw_files[:500]:
            remote_name = str(item.get("filename") or "")
            normalized = remote_name.replace("\\", "/")
            size = optional_count(item.get("size"))
            if not remote_name or size is None or extension(normalized) not in MEDIA:
                continue
            directory = shared_directory(remote_name)
            if directory in incomplete:
                continue
            key = (username, directory)
            folder = grouped.setdefault(key, [])
            # slskd rejects a batch that repeats a remote filename.
            # Keep the peer's original separators; Soulseek matches that string exactly.
            previous = next(
                (entry for entry in folder if entry.filename.replace("\\", "/") == normalized),
                None,
            )
            if previous:
                if size > previous.size:
                    previous.size = size
                    previous.filename = remote_name
            else:
                folder.append(SlskdFile(filename=remote_name, size=size))
            record = meta.setdefault(
                key,
                {
                    "queue": response.get("queueLength"),
                    "speed": response.get("uploadSpeed"),
                    "slot": response.get("hasFreeUploadSlot"),
                    "locked": False,
                },
            )
            record["locked"] = record["locked"] or directory in locked_dirs
    releases: list[SlskdRelease] = []
    for (username, directory), files in grouped.items():
        if len(files) > 400:
            continue
        shown = directory or leaf_name(files[0].filename)
        path_text = f"{directory} {' '.join(file.filename for file in files)}"
        matched_title = _matched([title], path_text)
        matched_authors = _matched(authors, path_text)
        identity = leaf_name(shown) or username
        releases.append(
            SlskdRelease(
                source_id=hashlib.sha256(f"{username}\n{directory}".encode()).hexdigest()[:40],
                raw_title=identity,
                title=title if matched_title else identity,
                authors=matched_authors,
                medium=folder_medium(files),
                formats=sorted({extension(file.filename).removeprefix(".") for file in files}),
                size_bytes=sum(file.size for file in files),
                seeders=None,
                protocol="soulseek",
                observed_at=observed_at,
                username=username,
                directory=directory,
                files=files,
                peer_online=True,
                queue_length=optional_count(meta[(username, directory)]["queue"]),
                upload_speed=optional_count(meta[(username, directory)]["speed"]),
                free_upload_slot=(
                    meta[(username, directory)]["slot"]
                    if isinstance(meta[(username, directory)]["slot"], bool)
                    else None
                ),
                search_id=search_id,
                locked_files=1 if meta[(username, directory)]["locked"] else 0,
            )
        )
    releases.sort(key=lambda item: (0 if "m4b" in item.formats else 1, -(item.size_bytes or 0)))
    return releases[:100]


# Characters slskd replaces on Linux or Windows. The destination we send has to survive both.
_REWRITTEN = set('<>:"|?*\\/\x00') | set(map(chr, range(32)))


def safe_segment(value: str) -> str:
    cleaned = "".join("_" if char in _REWRITTEN else char for char in value).strip(" .")
    if cleaned in {"", ".", ".."}:
        return ""
    return cleaned[:180]


def import_folder(release: SlskdRelease) -> str:
    """Folder slskd creates when the batch destination is set to the remote parent."""
    raw = safe_segment(leaf_name(release.directory)) if release.directory else ""
    return raw or "Soulseek"


def optional_guid(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def absolute_download_root(path: str) -> bool:
    if path.startswith("/") or path.startswith("\\\\"):
        return True
    return len(path) >= 3 and path[1] == ":" and path[2] in "\\/"


def search_settled(state: Any) -> bool:
    text = str((state or {}).get("state") or "").lower()
    if not text or "inprogress" in text:
        return False
    return "complete" in text.replace("incomplete", "")


def file_list_descriptor(release: SlskdRelease) -> tuple[TorrentDescriptor, bytes]:
    """Describe a Soulseek folder without pretending it is a torrent."""
    leaf = import_folder(release)
    paths: list[TorrentFile] = []
    seen: dict[str, int] = {}
    for item in release.files:
        base = leaf_name(item.filename) or "file"
        if base in {".", ".."}:
            base = "file"
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count:
            stem, dot, suffix = base.rpartition(".")
            base = f"{stem}-{count}.{suffix}" if dot else f"{base}-{count}"
        # slskd 0.26 writes the batch destination under the downloads directory.
        relative = f"{leaf}/{base}"
        paths.append(TorrentFile(index=len(paths), path=relative[:2048], size_bytes=item.size))
    content = json.dumps(
        {
            "username": release.username,
            "directory": release.directory,
            "files": [file.model_dump() for file in release.files],
        },
        sort_keys=True,
    ).encode()
    total = sum(item.size for item in release.files)
    return (
        TorrentDescriptor(
            name=leaf[:255],
            artifact_sha256=hashlib.sha256(content).hexdigest(),
            private=False,
            content_bytes=max(total, 1),
            torrent_bytes=max(total, 1),
            padding_bytes=0,
            files=paths,
            parser="slskd-file-list",
        ),
        content,
    )


def _flags(value: Any) -> set[str]:
    text = str(value or "").replace("|", ",")
    return {part.strip().lower() for part in text.split(",") if part.strip()}


def transfer_finished(state: Any) -> bool:
    flags = _flags(state)
    return bool(flags & COMPLETE) and not (flags & FAILED)


def transfer_failed(state: Any) -> bool:
    return bool(_flags(state) & FAILED)


def match_file(
    files: list[SlskdFile], remaining: set[int], remote: str, size: int | None
) -> int | None:
    """Match a transfer to the original shared name, not the renamed import path."""
    base = leaf_name(remote)
    if size is not None:
        for index in remaining:
            if files[index].filename.replace("\\", "/") == remote and files[index].size == size:
                return index
        for index in remaining:
            if leaf_name(files[index].filename) == base and files[index].size == size:
                return index
        return None
    for index in remaining:
        if leaf_name(files[index].filename) == base:
            return index
    return None


class SlskdClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 20, transport=None) -> None:
        import httpx

        self.base_url = f"{configured_url(base_url).rstrip('/')}/api/v0"
        self.api_key = api_key
        self.timeout = timeout
        self.status = 0
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> "SlskdClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        import httpx

        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as error:
            raise AdapterError(FailureKind.TIMEOUT, "slskd timed out") from error
        except httpx.HTTPError as error:
            raise AdapterError(FailureKind.UNAVAILABLE, "slskd is unavailable") from error
        if response.status_code == 401:
            raise AdapterError(FailureKind.AUTHENTICATION, "slskd rejected this API key")
        if response.status_code == 403:
            raise AdapterError(
                FailureKind.AUTHENTICATION,
                "slskd refused this call; use a read-write key on the main slskd process",
            )
        if response.status_code == 404:
            raise AdapterError(FailureKind.NOT_FOUND, "slskd could not find that peer or batch")
        if response.status_code == 409:
            raise AdapterError(FailureKind.UNCERTAIN, "slskd already has this download batch")
        if response.status_code == 429:
            raise AdapterError(
                FailureKind.RATE_LIMIT,
                "slskd is busy with another search or download",
                retry_after=5,
            )
        if response.status_code == 400 and path.startswith("/transfers/downloads"):
            raise AdapterError(FailureKind.UNSUPPORTED, "Soulseek rejected this folder")
        if response.status_code >= 400:
            raise AdapterError(FailureKind.PARSER, f"slskd returned HTTP {response.status_code}")
        self.status = response.status_code
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise AdapterError(
                FailureKind.PARSER, "slskd returned an unexpected response"
            ) from error

    async def test(self) -> dict[str, str]:
        application = await self.request("GET", "/application")
        version = await self.request("GET", "/application/version")
        try:
            options = await self.request("GET", "/options")
        except AdapterError as error:
            # slskd 0.26 shows its options, with the download folder, only to administrator keys.
            if error.kind is not FailureKind.AUTHENTICATION:
                raise
            raise AdapterError(
                FailureKind.AUTHENTICATION,
                "slskd only shares its download folder with an administrator API key. In slskd, "
                "give this key the administrator role, then test again.",
            ) from error
        if not soulseek_connected(application):
            raise AdapterError(FailureKind.UNAVAILABLE, "Soulseek is not connected in slskd")
        downloads = ((options or {}).get("directories") or {}).get("downloads")
        if not isinstance(downloads, str) or not absolute_download_root(downloads):
            raise AdapterError(FailureKind.UNSUPPORTED, "slskd did not report a download directory")
        return {"version": version_text(version), "download_root": downloads}

    async def search(
        self,
        query: str,
        *,
        title: str,
        authors: list[str],
        observed_at: datetime,
    ) -> list[SlskdRelease]:
        import asyncio

        created = await self.request(
            "POST",
            "/searches",
            json={
                "searchText": query[:200],
                "searchTimeout": SEARCH_TIMEOUT_MS,
                "fileLimit": 10000,
                "filterResponses": True,
                "maximumPeerQueueLength": 1000,
            },
        )
        search_id = str((created or {}).get("id") or "")
        if not search_id:
            raise AdapterError(FailureKind.PARSER, "slskd did not return a search id")
        try:
            deadline = asyncio.get_running_loop().time() + SEARCH_BUDGET_SECONDS
            state = created
            while not search_settled(state):
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(1)
                state = await self.request("GET", f"/searches/{quote(search_id, safe='')}")
            responses = await self.request(
                "GET", f"/searches/{quote(search_id, safe='')}/responses"
            )
        finally:
            try:
                await self.request("DELETE", f"/searches/{quote(search_id, safe='')}")
            except AdapterError:
                pass
        if not isinstance(responses, list):
            raise AdapterError(FailureKind.PARSER, "slskd search responses were not a list")
        return group_responses(
            responses,
            search_id=search_id,
            observed_at=observed_at,
            title=title,
            authors=authors,
        )

    async def enqueue(self, release: SlskdRelease, *, attempt_id: str) -> str:
        options = {"externalId": attempt_id, "destination": import_folder(release)}
        payload = {
            "id": attempt_id,
            "username": release.username,
            "files": [{"filename": item.filename, "size": item.size} for item in release.files],
            "options": options,
        }
        search_id = optional_guid(release.search_id)
        if search_id:
            payload["searchId"] = search_id
        body = await self.request("POST", "/transfers/downloads/batches", json=payload)
        accepted = 0
        failures: list[Any] = []
        if isinstance(body, dict):
            transfers = (body.get("batch") or {}).get("transfers") or body.get("transfers") or []
            accepted = len(transfers)
            failures = list(body.get("failures") or [])
        if self.status != 201 or failures or accepted != len(release.files):
            try:
                await self.cancel(release.username, attempt_id)
            except AdapterError:
                pass
            if self.status == 404:
                raise AdapterError(FailureKind.NOT_FOUND, "The Soulseek peer looks offline")
            raise AdapterError(
                FailureKind.UNSUPPORTED,
                "Soulseek queued only part of this folder",
            )
        return attempt_id

    async def batch(self, batch_id: str, release: SlskdRelease) -> SlskdState:
        body = await self.request("GET", f"/transfers/downloads/batches/{quote(batch_id, safe='')}")
        transfers = []
        if isinstance(body, dict):
            transfers = (body.get("batch") or body).get("transfers") or []
        if not isinstance(transfers, list):
            raise AdapterError(FailureKind.PARSER, "slskd batch transfers were not a list")
        descriptor, _content = file_list_descriptor(release)
        remaining = set(range(len(release.files)))
        files: list[DownloadFile] = []
        complete = bool(transfers) and len(transfers) == len(descriptor.files)
        failed_transfer = False
        for transfer in transfers:
            remote = str(transfer.get("filename") or "").replace("\\", "/")
            size = optional_count(transfer.get("size"))
            done = transfer_finished(transfer.get("state"))
            failed = transfer_failed(transfer.get("state"))
            if failed:
                failed_transfer = True
            index = match_file(release.files, remaining, remote, size)
            if done and size is not None and index is None:
                failed_transfer = True
            if index is None or failed or not done or size is None:
                complete = False
            if index is not None:
                remaining.discard(index)
                described = descriptor.files[index]
                files.append(
                    DownloadFile(
                        relative_path=described.path,
                        size_bytes=described.size_bytes,
                        complete=done and not failed and size is not None,
                    )
                )
        if remaining or len(files) != len(descriptor.files):
            complete = False
        # One failed file means this folder can never match the saved file list.
        if failed_transfer:
            complete = False
        return SlskdState(
            external_id=batch_id,
            state="failed" if failed_transfer else "complete" if complete else "downloading",
            completed=complete,
            save_path="",
            files=files,
            association_verified=True,
            reported_complete=complete,
            total_bytes=descriptor.torrent_bytes if complete else None,
        )

    async def cancel(self, username: str, batch_id: str) -> None:
        """Cancel each transfer. The delete route takes a transfer id."""
        try:
            body = await self.request(
                "GET", f"/transfers/downloads/batches/{quote(batch_id, safe='')}"
            )
        except AdapterError as error:
            if error.kind == FailureKind.NOT_FOUND:
                return
            raise
        transfers = []
        if isinstance(body, dict):
            transfers = (body.get("batch") or body).get("transfers") or []
        encoded = quote(username, safe="")
        for transfer in transfers:
            identifier = str(transfer.get("id") or "")
            if not identifier:
                continue
            try:
                await self.request(
                    "DELETE",
                    f"/transfers/downloads/{encoded}/{quote(identifier, safe='')}",
                    params={"remove": "true"},
                )
            except AdapterError:
                continue
