"""Serialized, generation-fenced native ABB observations and torrent resolution."""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.adapters.audiobookbay import ABBClient, ABBRelease
from app.adapters.contracts import AdapterError, FailureKind
from app.db.models import SourceConnection
from app.db.session import session_factory
from app.domain.operations import transaction_lock
from app.domain.source_network import check_actor
from app.security import decrypt_secrets

REQUEST_INTERVAL = 4.0


async def abb_call(
    user_id, operation, argument=None, *, expected_generation=None, recovery_guard=None
):
    if operation not in {"test", "search", "detail"}:
        raise ValueError("Unsupported ABB observation")
    token = uuid4()
    async with session_factory()() as db, db.begin():
        await check_actor(db, user_id, admin=operation == "test")
        await transaction_lock(db, "source:audiobookbay")
        if recovery_guard is not None:
            await recovery_guard(db)
        row = await db.get(SourceConnection, "audiobookbay")
        if not row or not row.enabled:
            raise HTTPException(409, "An administrator must connect and enable AudiobookBay first")
        if expected_generation is not None and row.generation != expected_generation:
            raise HTTPException(409, "AudiobookBay settings changed. Search again.")
        now = datetime.now(UTC)
        if row.lease_token and row.lease_until and row.lease_until > now:
            raise AdapterError(
                FailureKind.RATE_LIMIT, "AudiobookBay is handling another request.", retry_after=2
            )
        due = max(now, row.next_request_at or now, row.blocked_until or now)
        wait = (due - now).total_seconds()
        if wait > 5:
            raise AdapterError(
                FailureKind.RATE_LIMIT, "AudiobookBay is cooling down.", retry_after=math.ceil(wait)
            )
        row.lease_token, row.lease_until = token, now + timedelta(seconds=120)
        row.next_request_at = due + timedelta(seconds=REQUEST_INTERVAL)
        generation, endpoint, proxy = row.generation, row.base_url, row.proxy_url
        secrets = decrypt_secrets(row.encrypted_secrets)
    client, failure, value = None, None, None
    try:
        if wait:
            await asyncio.sleep(wait)
        async with (
            asyncio.timeout(100),
            ABBClient(
                endpoint,
                proxy_url=proxy,
                proxy_username=secrets.get("proxy_username"),
                proxy_password=secrets.get("proxy_password"),
            ) as client,
        ):
            value = (
                await client.test()
                if operation == "test"
                else await getattr(client, operation)(argument)
            )
    except AdapterError as error:
        failure = error
    except TimeoutError:
        failure = AdapterError(FailureKind.TIMEOUT, "AudiobookBay request timed out.")
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, "source:audiobookbay")
        row = await db.get(SourceConnection, "audiobookbay")
        if not row or row.lease_token != token:
            raise HTTPException(409, "AudiobookBay request expired. Retry with current settings.")
        row.lease_token, row.lease_until = None, None
        row.next_request_at = datetime.now(UTC) + timedelta(seconds=REQUEST_INTERVAL)
        if client and client.cooldown:
            due = datetime.now(UTC) + timedelta(seconds=client.cooldown)
            row.blocked_until = max(row.blocked_until or due, due)
        changed = row.generation != generation or not row.enabled
        if not changed:
            row.last_checked_at = datetime.now(UTC)
            row.status = failure.kind.value if failure else "connected"
            row.last_error = str(failure) if failure else None
            if not failure:
                row.last_success_at = datetime.now(UTC)
    if changed:
        raise HTTPException(409, "AudiobookBay settings changed during this request. Search again.")
    async with session_factory()() as db:
        await check_actor(db, user_id, admin=operation == "test")
    if failure:
        raise failure
    return value, generation


@dataclass
class ABBArtifact:
    release: ABBRelease
    content: bytes = field(repr=False)


async def resolve_abb(
    user_id, release, *, expected_generation, downloader_id=None, downloader_generation=None
):
    from app.domain.downloaders import resolve_metadata
    from app.domain.source_artifacts import member, persist_artifact

    async with session_factory()() as db:
        await member(db, user_id)
        source = await db.get(SourceConnection, "audiobookbay")
        if not source or not source.enabled or source.generation != expected_generation:
            raise HTTPException(409, "AudiobookBay settings changed. Search again.")
        configured = decrypt_secrets(source.encrypted_secrets).get("metadata_downloader_id")
        downloader_id = downloader_id or (UUID(configured) if configured else None)
        if not downloader_id:
            raise HTTPException(409, "Configure a qBittorrent metadata resolver for AudiobookBay")
    detail, generation = await abb_call(
        user_id, "detail", release.detail_path, expected_generation=expected_generation
    )
    if detail.release.source_id != release.source_id or not detail.magnet:
        raise AdapterError(
            FailureKind.UNSUPPORTED, "This posting has no usable matching torrent identity."
        )
    content, downloader_generation = await resolve_metadata(
        user_id, downloader_id, detail.magnet, expected_generation=downloader_generation
    )
    detail.release.metadata_resolved = True
    artifact = ABBArtifact(detail.release, content)
    identifier = await persist_artifact(
        user_id,
        detail.release.source_id,
        artifact,
        generation,
        "audiobookbay",
        expected_downloader=(downloader_id, downloader_generation),
    )
    return identifier, detail.release
