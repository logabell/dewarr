"""Downloader settings and read-only diagnostics; dispatch has a separate lifecycle."""

import asyncio
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.deluge import DelugeClient
from app.adapters.nzbget import NzbClient
from app.adapters.qbittorrent import QbitClient, absolute_path
from app.adapters.sabnzbd import SabClient
from app.adapters.transmission import TransmissionClient
from app.db.models import ImportStorageSettings, Integration
from app.db.session import session_factory
from app.domain.download_folders import browse_roots, readable_folder
from app.domain.operations import transaction_lock
from app.domain.source_network import check_actor
from app.security import decrypt_secrets

TORRENT_KINDS = {"qbittorrent", "transmission", "deluge"}
DOWNLOAD_KINDS = {*TORRENT_KINDS, "sabnzbd", "nzbget"}
TRANSFER_KINDS = {*DOWNLOAD_KINDS, "slskd"}
USENET_KINDS = {"sabnzbd", "nzbget"}


def client_protocol(kind):
    if kind in USENET_KINDS:
        return "nzb"
    if kind in TORRENT_KINDS:
        return "torrent"
    if kind == "slskd":
        return "soulseek"
    return None


def client_features(row):
    operations = row.capabilities.get("operations", [])
    return {
        "attempt_tagging": row.kind == "qbittorrent" or "attempt-tagging" in operations,
        "in_client_rename": row.kind == "qbittorrent",
        "categories": row.kind != "deluge" or "categories" in operations,
        "sequential_first_last": False,
        "full_v2_hashes": row.kind == "qbittorrent",
        "magnet_metadata": "magnet-metadata" in operations,
    }


SETTINGS_LOCK = "downloaders:settings"
TEST_INTERVAL = 2
TEST_TIMEOUT = 60
TEST_LEASE_SECONDS = 90


class DownloadMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    download_root: str = Field(max_length=2000)
    source_key: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,60}$")
    worker_path: str | None = Field(default=None, max_length=2000)

    @field_validator("download_root", "worker_path")
    @classmethod
    def root(cls, value):
        return absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def target(self):
        if self.source_key is None and self.worker_path is None:
            raise ValueError("Choose the folder Dewarr can read")
        return self


def relative_to(path, root):
    try:
        relative = PurePosixPath(path).relative_to(root)
        return "" if str(relative) == "." else str(relative)
    except ValueError:
        return None


def overlaps(path: Path, other: Path) -> bool:
    return path == other or path.is_relative_to(other) or other.is_relative_to(path)


def library_conflict(path: Path, settings) -> bool:
    others = list(settings.import_destinations.values())
    if settings.import_staging_root:
        others.append(settings.import_staging_root)
    return any(overlaps(path, other) for other in others)


def allocate_key(path: Path, sources) -> str:
    for key, root in sources.items():
        if Path(root) == path:
            return key
    stem = re.sub(r"[^a-z0-9_-]+", "", path.name.lower()).strip("-_") or "downloads"
    if not re.fullmatch(r"[a-z0-9_-]{1,60}", stem):
        stem = "downloads"
    stem = stem[:60]
    candidate, number = stem, 2
    while candidate in sources:
        suffix = f"-{number}"
        candidate = stem[: 60 - len(suffix)] + suffix
        number += 1
        if number > 100:
            raise HTTPException(422, "Too many download folders use that name")
    return candidate


def bind_mappings(
    mappings: list[DownloadMapping], save_path: str, sources
) -> tuple[list[dict], dict]:
    """Return saved mappings and any new worker folders that must be remembered."""
    sources, declared, bound = dict(sources), {}, []
    for mapping in mappings:
        if mapping.worker_path and (
            not mapping.source_key or str(sources.get(mapping.source_key)) != mapping.worker_path
        ):
            root = Path(mapping.worker_path)
            for existing in sources.values():
                existing = Path(existing)
                if existing != root and overlaps(root, existing):
                    raise HTTPException(422, "Download folders must not overlap")
            key = mapping.source_key or allocate_key(root, sources)
            if key in sources and Path(sources[key]) != root:
                raise HTTPException(422, "That download folder name is already used")
            if key not in sources:
                sources[key] = root
                declared[key] = str(root)
        else:
            root = sources.get(mapping.source_key)
            if not root:
                raise HTTPException(422, "Select a download root configured on the worker")
            key = mapping.source_key
        if any(
            relative_to(mapping.download_root, previous["download_root"]) is not None
            or relative_to(previous["download_root"], mapping.download_root) is not None
            for previous in bound
        ):
            raise HTTPException(422, "Download path mappings must not overlap")
        bound.append(
            {
                "download_root": mapping.download_root,
                "source_key": key,
                "source_path": str(sources[key]),
            }
        )
    if not any(relative_to(save_path, mapping["download_root"]) is not None for mapping in bound):
        raise HTTPException(422, "The save path must be inside a mapped download root")
    return bound, declared


def mappings_current(row, sources):
    mappings = row.config.get("mappings", [])
    if row.config.get("client_managed") and not any(
        relative_to(row.config.get("save_path", ""), mapping["download_root"]) is not None
        for mapping in mappings
    ):
        return False
    return bool(mappings) and all(
        str(sources.get(mapping["source_key"])) == mapping["source_path"] for mapping in mappings
    )


def mapped_path(row, path, sources):
    try:
        path = absolute_path(path)
    except ValueError as error:
        raise HTTPException(422, "The download path does not match one configured root") from error
    if not mappings_current(row, sources):
        raise HTTPException(
            409, "Worker download roots changed. Review and save the path mappings."
        )
    matches = []
    for mapping in row.config["mappings"]:
        relative = relative_to(path, mapping["download_root"])
        if relative is not None:
            matches.append(
                {
                    "download_path": path,
                    "source_key": mapping["source_key"],
                    "relative_path": relative,
                    "worker_path": str(PurePosixPath(mapping["source_path"]) / relative),
                }
            )
    if len(matches) != 1:
        raise HTTPException(422, "The download path does not match one configured root")
    return matches[0]


async def remember_download_root(db, row, observed_path):
    """Bind identical visible paths; never guess a remote-to-local translation."""
    mappings = row.config.get("mappings", [])
    try:
        observed_path = absolute_path(observed_path)
    except ValueError:
        # A remote path dialect must not become a local root by accident.
        row.config = {**row.config, "save_path": observed_path}
        return
    if not mappings:
        from app.importing.storage import storage_settings

        settings = await storage_settings(db)
        sources = settings.import_sources
        candidates = [
            (key, str(root))
            for key, root in sources.items()
            if relative_to(observed_path, str(root)) is not None
        ]
        if candidates:
            key, root = max(candidates, key=lambda item: len(item[1]))
            mappings = [
                {
                    "download_root": root,
                    "source_key": key,
                    "source_path": root,
                }
            ]
        else:
            # Never guess a remote-to-local translation from folder names.
            roots = await asyncio.to_thread(browse_roots, settings)
            readable = await asyncio.to_thread(readable_folder, observed_path, roots)
            path = Path(observed_path)
            if (
                readable
                and not library_conflict(path, settings)
                and not any(overlaps(path, Path(root)) for root in sources.values())
            ):
                key = allocate_key(path, sources)
                storage = await db.get(ImportStorageSettings, 1, with_for_update=True)
                if not storage:
                    storage = ImportStorageSettings(id=1, destinations={}, sources={})
                    db.add(storage)
                storage.sources = {**storage.sources, key: observed_path}
                mappings = [
                    {
                        "download_root": observed_path,
                        "source_key": key,
                        "source_path": observed_path,
                    }
                ]
    row.config = {**row.config, "save_path": observed_path, "mappings": mappings}


async def connection_or_404(db, connection_id):
    row = await db.get(Integration, connection_id, populate_existing=True)
    if not row or row.deleted_at or row.kind not in DOWNLOAD_KINDS or row.owner_id is not None:
        raise HTTPException(404, "Downloader connection not found")
    return row


async def transfer_connection(db, connection_id):
    """Saved download client, including Soulseek. Settings tests stay on connection_or_404."""
    row = await db.get(Integration, connection_id, populate_existing=True)
    if not row or row.deleted_at or row.kind not in TRANSFER_KINDS or row.owner_id is not None:
        raise HTTPException(404, "Downloader connection not found")
    return row


async def test_connection(user_id, connection_id):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, SETTINGS_LOCK)
        await check_actor(db, user_id, admin=True)
        row = await connection_or_404(db, connection_id)
        if not row.enabled:
            raise HTTPException(409, "Enable the downloader before testing it")
        now = datetime.now(UTC)
        if row.lease_until and row.lease_until > now:
            raise AdapterError(
                FailureKind.RATE_LIMIT, "A connection test is running.", retry_after=2
            )
        if row.next_sync_at and row.next_sync_at > now:
            raise AdapterError(
                FailureKind.RATE_LIMIT,
                "Wait before testing this downloader again.",
                retry_after=math.ceil((row.next_sync_at - now).total_seconds()),
            )
        generation, endpoint, kind = row.credential_generation, row.base_url, row.kind
        client_managed, category = row.config.get("client_managed", False), row.config["category"]
        credentials = decrypt_secrets(row.encrypted_secrets)
        row.lease_token, row.lease_until = token, now + timedelta(seconds=TEST_LEASE_SECONDS)
        row.next_sync_at = now + timedelta(seconds=TEST_INTERVAL)
    failure = None
    capabilities = None
    observed_path = None
    try:
        if kind == "sabnzbd":
            client = SabClient(endpoint, credentials.get("api_key", ""))
        elif kind == "nzbget":
            client = NzbClient(
                endpoint,
                credentials.get("username", ""),
                credentials.get("password", ""),
            )
        elif kind == "transmission":
            client = TransmissionClient(
                endpoint, credentials.get("username", ""), credentials.get("password", "")
            )
        elif kind == "deluge":
            client = DelugeClient(
                endpoint, credentials.get("username", ""), credentials.get("password", "")
            )
        else:
            client = QbitClient(endpoint, credentials["username"], credentials["password"])
        async with asyncio.timeout(TEST_TIMEOUT), client:
            capabilities = await client.capabilities()
            if client_managed:
                observed_path = await client.download_location(category)
    except AdapterError as error:
        failure = error
    except TimeoutError:
        failure = AdapterError(FailureKind.TIMEOUT, "The downloader connection test timed out.")
    # Cancellation/crash leaves a bounded lease. Unlike rotating MAM sessions,
    # a read-only qBit test can be retried after it expires using a fresh login.
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, SETTINGS_LOCK)
        row = await connection_or_404(db, connection_id)
        if row.lease_token != token:
            raise HTTPException(409, "A newer downloader test superseded this result")
        row.lease_token, row.lease_until = None, None
        changed = row.credential_generation != generation or not row.enabled
        if not changed:
            row.last_checked_at = datetime.now(UTC)
            row.status = failure.kind.value if failure else "connected"
            row.last_error = str(failure) if failure else None
            row.capabilities = capabilities.model_dump(mode="json") if capabilities else {}
            if not failure:
                row.last_success_at = datetime.now(UTC)
                if observed_path is not None:
                    await remember_download_root(db, row, observed_path)
        if failure:
            row.next_sync_at = datetime.now(UTC) + timedelta(
                seconds=max(60, failure.retry_after or 0)
            )
    async with session_factory()() as db:
        await check_actor(db, user_id, admin=True)
    if changed:
        raise HTTPException(
            409, "Downloader settings changed during the test; test the saved settings"
        )
    if failure:
        raise failure


async def resolve_metadata(user_id, connection_id, magnet, *, expected_generation=None):
    """Bounded read-only metadata inspection sharing the diagnostic connection lease."""
    from app.domain.source_artifacts import member

    token = uuid4()
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, SETTINGS_LOCK)
        await member(db, user_id)
        row = await connection_or_404(db, connection_id)
        if not row.enabled or (
            expected_generation is not None and row.credential_generation != expected_generation
        ):
            raise HTTPException(409, "Downloader settings changed; select its current connection")
        now = datetime.now(UTC)
        if row.lease_until and row.lease_until > now:
            raise AdapterError(
                FailureKind.RATE_LIMIT, "Downloader inspection is busy.", retry_after=2
            )
        if row.next_sync_at and row.next_sync_at > now:
            raise AdapterError(
                FailureKind.RATE_LIMIT,
                "Downloader inspection is cooling down.",
                retry_after=math.ceil((row.next_sync_at - now).total_seconds()),
            )
        if row.kind != "qbittorrent":
            raise HTTPException(422, "Magnet inspection requires qBittorrent")
        generation, endpoint = row.credential_generation, row.base_url
        credentials = decrypt_secrets(row.encrypted_secrets)
        row.lease_token, row.lease_until = token, now + timedelta(seconds=90)
        row.next_sync_at = now + timedelta(seconds=TEST_INTERVAL)
    failure, content = None, None
    try:
        async with (
            asyncio.timeout(60),
            QbitClient(endpoint, credentials["username"], credentials["password"]) as client,
        ):
            content = await client.resolve_magnet(magnet)
    except AdapterError as error:
        failure = error
    except TimeoutError:
        failure = AdapterError(FailureKind.TIMEOUT, "Torrent metadata inspection timed out.")
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, SETTINGS_LOCK)
        row = await connection_or_404(db, connection_id)
        if row.lease_token != token:
            raise HTTPException(409, "Downloader inspection expired; retry current settings")
        row.lease_token, row.lease_until = None, None
        changed = row.credential_generation != generation or not row.enabled
        row.next_sync_at = datetime.now(UTC) + timedelta(
            seconds=max(TEST_INTERVAL, failure.retry_after or 0) if failure else TEST_INTERVAL
        )
        # A torrent lacking peers does not imply the entire downloader is broken.
    async with session_factory()() as db:
        await member(db, user_id)
    if changed:
        raise HTTPException(409, "Downloader changed during metadata inspection; retry")
    if failure:
        raise failure
    return content, generation
