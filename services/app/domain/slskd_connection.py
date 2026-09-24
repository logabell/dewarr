"""Soulseek settings live on a source connection and the slskd downloader together."""

import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.slskd import SEARCH_BUDGET_SECONDS, SEARCH_CALL_TIMEOUT, SlskdClient
from app.db.models import AuditEvent, Integration, SourceConnection
from app.db.session import session_factory
from app.domain.downloaders import SETTINGS_LOCK, remember_download_root
from app.domain.operations import transaction_lock
from app.domain.source_network import check_actor
from app.security import decrypt_secrets, encrypt_secrets

# Cover a timed-out create, the search poll, the response fetch, and the delete.
LEASE_SECONDS = SEARCH_CALL_TIMEOUT * 3 + SEARCH_BUDGET_SECONDS + 15
TEST_INTERVAL = 2


def blank_config():
    return {"save_path": "", "category": "", "mappings": [], "client_managed": True}


async def integration(db):
    return await db.scalar(
        select(Integration)
        .where(
            Integration.kind == "slskd",
            Integration.owner_id.is_(None),
            Integration.deleted_at.is_(None),
        )
        .order_by(Integration.created_at)
        .limit(1)
    )


async def save(db, admin, body):
    await transaction_lock(db, SETTINGS_LOCK)
    await transaction_lock(db, "source:slskd")
    source = await db.get(SourceConnection, "slskd")
    client = await integration(db)
    current = source.generation if source else 0
    if current != body.expected_generation:
        raise HTTPException(409, "Soulseek settings changed. Reload before saving.")
    secrets = decrypt_secrets(source.encrypted_secrets) if source else {}
    if (not source or source.base_url != body.base_url) and not body.api_key:
        raise HTTPException(422, "Enter an API key when connecting slskd")
    if body.api_key:
        secrets["api_key"] = body.api_key.get_secret_value()
    if "api_key" not in secrets:
        raise HTTPException(422, "Enter an API key when connecting slskd")
    if not source:
        source = SourceConnection(key="slskd", generation=0)
        db.add(source)
    if not client:
        client = Integration(kind="slskd", name="Soulseek", credential_generation=0)
        db.add(client)
    encoded = encrypt_secrets({"api_key": secrets["api_key"]})
    source.deleted_at = None
    source.base_url = body.base_url
    source.enabled = body.enabled
    source.encrypted_secrets = encoded
    source.generation += 1
    source.status, source.last_error, source.last_success_at = "untested", None, None
    previous_url = client.__dict__.get("base_url")
    client.name = "Soulseek"
    client.base_url = body.base_url
    client.enabled = body.enabled
    client.encrypted_secrets = encoded
    client.config = (
        {**blank_config(), **(client.config or {})}
        if previous_url == body.base_url
        else blank_config()
    )
    client.credential_generation += 1
    client.capabilities = {}
    client.status, client.last_error, client.last_success_at = "untested", None, None
    await db.flush()
    db.add(AuditEvent(actor_id=admin.id, action="source.slskd.updated", entity_id=client.id))
    return source, client


async def test_connection(user_id):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, SETTINGS_LOCK)
        await transaction_lock(db, "source:slskd")
        await check_actor(db, user_id, admin=True)
        source = await db.get(SourceConnection, "slskd")
        client_row = await integration(db)
        if not source or not client_row or not source.enabled:
            raise HTTPException(409, "Save and enable Soulseek before testing it")
        now = datetime.now(UTC)
        if source.lease_until and source.lease_until > now:
            raise AdapterError(FailureKind.RATE_LIMIT, "A Soulseek test is running.", retry_after=2)
        if source.next_request_at and source.next_request_at > now:
            raise AdapterError(
                FailureKind.RATE_LIMIT,
                "Wait before testing Soulseek again.",
                retry_after=math.ceil((source.next_request_at - now).total_seconds()),
            )
        generation, endpoint = source.generation, source.base_url
        api_key = decrypt_secrets(source.encrypted_secrets).get("api_key")
        if not api_key:
            raise HTTPException(422, "Enter an API key when connecting slskd")
        source.lease_token = token
        source.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        source.next_request_at = now + timedelta(seconds=TEST_INTERVAL)
    failure = None
    observed = None
    try:
        async with SlskdClient(endpoint, api_key) as client:
            observed = await client.test()
    except AdapterError as error:
        failure = error
    except TimeoutError:
        failure = AdapterError(FailureKind.TIMEOUT, "slskd timed out")
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, SETTINGS_LOCK)
        await transaction_lock(db, "source:slskd")
        source = await db.get(SourceConnection, "slskd")
        client_row = await integration(db)
        if not source or not client_row or source.lease_token != token:
            raise HTTPException(409, "A newer Soulseek test superseded this result")
        source.lease_token, source.lease_until = None, None
        changed = source.generation != generation or not source.enabled
        status = failure.kind.value if failure else "connected"
        if not changed:
            source.status = client_row.status = status
            source.last_error = client_row.last_error = str(failure) if failure else None
            if not failure and observed:
                source.last_success_at = client_row.last_success_at = datetime.now(UTC)
                client_row.capabilities = {
                    "protocols": ["soulseek"],
                    "version": observed["version"],
                }
                await remember_download_root(db, client_row, observed["download_root"])
        if failure:
            source.next_request_at = datetime.now(UTC) + timedelta(
                seconds=max(60, failure.retry_after or 0)
            )
    async with session_factory()() as db:
        await check_actor(db, user_id, admin=True)
    if changed:
        raise HTTPException(
            409, "Soulseek settings changed during the test; test the saved settings"
        )
    if failure:
        raise failure


async def search(user_id, argument, *, expected_generation):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, "source:slskd")
        await check_actor(db, user_id)
        source = await db.get(SourceConnection, "slskd")
        if not source or not source.enabled or source.generation != expected_generation:
            raise HTTPException(409, "Soulseek settings changed. Start a new search.")
        now = datetime.now(UTC)
        if source.lease_until and source.lease_until > now:
            raise AdapterError(
                FailureKind.RATE_LIMIT,
                "Soulseek is busy with another search.",
                retry_after=2,
            )
        endpoint = source.base_url
        generation = source.generation
        api_key = decrypt_secrets(source.encrypted_secrets).get("api_key")
        if not api_key:
            raise HTTPException(422, "Enter an API key when connecting slskd")
        source.lease_token = token
        source.lease_until = now + timedelta(seconds=LEASE_SECONDS)
    failure = None
    releases = None
    try:
        async with SlskdClient(endpoint, api_key, timeout=SEARCH_CALL_TIMEOUT) as client:
            releases = await client.search(
                argument["q"],
                title=argument["title"],
                authors=argument["authors"],
                observed_at=argument["observed_at"],
            )
    except AdapterError as error:
        failure = error
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, "source:slskd")
        source = await db.get(SourceConnection, "slskd")
        if not source or source.lease_token != token:
            raise HTTPException(409, "Soulseek settings changed during this search")
        source.lease_token, source.lease_until = None, None
        changed = source.generation != generation or not source.enabled
        if not changed:
            source.status = failure.kind.value if failure else "connected"
            source.last_error = str(failure) if failure else None
            if not failure:
                source.last_success_at = datetime.now(UTC)
    if changed:
        raise HTTPException(409, "Soulseek settings changed during this search")
    if failure:
        raise failure
    return releases, generation
