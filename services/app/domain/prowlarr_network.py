"""Credential-scoped Prowlarr budget and generation fences around read-only I/O."""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.prowlarr import ProwlarrClient
from app.db.models import SourceConnection
from app.db.session import session_factory
from app.domain.operations import transaction_lock
from app.domain.source_network import check_actor
from app.security import decrypt_secrets

REQUEST_INTERVAL = 1.0


async def prowlarr_call(
    user_id, operation, argument=None, *, expected_generation=None, recovery_guard=None
):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        await check_actor(db, user_id, admin=operation == "test")
        await transaction_lock(db, "source:prowlarr")
        if recovery_guard is not None:
            await recovery_guard(db)
        row = await db.get(SourceConnection, "prowlarr")
        if not row or not row.enabled:
            raise HTTPException(409, "An administrator must connect and enable Prowlarr first")
        if expected_generation is not None and expected_generation != row.generation:
            raise HTTPException(409, "Prowlarr settings changed. Search again.")
        now = datetime.now(UTC)
        if row.lease_token and row.lease_until and row.lease_until > now:
            raise AdapterError(
                FailureKind.RATE_LIMIT, "Prowlarr is handling another request.", retry_after=2
            )
        due = max(now, row.next_request_at or now, row.blocked_until or now)
        wait = (due - now).total_seconds()
        if wait > 5:
            raise AdapterError(
                FailureKind.RATE_LIMIT, "Prowlarr is cooling down.", retry_after=math.ceil(wait)
            )
        # A dead read-only API-key request can be retried after lease expiry.
        row.lease_token, row.lease_until = token, now + timedelta(seconds=150)
        row.next_request_at = due + timedelta(seconds=REQUEST_INTERVAL)
        generation, endpoint = row.generation, row.base_url
        secrets = decrypt_secrets(row.encrypted_secrets)
        mam = await db.get(SourceConnection, "mam")
        native_mam = bool(mam and mam.enabled)
    client, failure, value = None, None, None
    try:
        if wait:
            await asyncio.sleep(wait)
        async with ProwlarrClient(endpoint, secrets["api_key"]) as client:
            if operation in {"indexers", "search", "resolve"}:
                indexers = await client.indexers()
                excluded = set(secrets.get("excluded_indexers", []))
                for indexer in indexers:
                    indexer.excluded = indexer.id in excluded or (native_mam and indexer.native_mam)
                if operation == "indexers":
                    value = indexers
                else:
                    indexer_id = (
                        argument.indexer_id
                        if operation == "search"
                        else int(argument[0].indexer_id)
                    )
                    indexer = next((item for item in indexers if item.id == indexer_id), None)
                    if (
                        not indexer
                        or not indexer.enabled
                        or not indexer.supports_search
                        or indexer.excluded
                    ):
                        raise AdapterError(
                            FailureKind.UNSUPPORTED,
                            "This indexer is disabled, excluded, or handled by native MAM.",
                        )
                    if (
                        operation == "search"
                        and argument.offset
                        and not indexer.supports_pagination
                    ):
                        raise AdapterError(
                            FailureKind.UNSUPPORTED, "This indexer does not support paging."
                        )
                    value = await getattr(client, operation)(argument)
            else:
                value = await client.test()
    except AdapterError as error:
        failure = error
    # Cancellation leaves a bounded lease. A late response cannot clear a newer lease.
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, "source:prowlarr")
        row = await db.get(SourceConnection, "prowlarr")
        if not row or row.lease_token != token:
            raise HTTPException(409, "Prowlarr request expired. Retry with current settings.")
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
        raise HTTPException(409, "Prowlarr settings changed during this request. Search again.")
    async with session_factory()() as db:
        await check_actor(db, user_id, admin=operation == "test")
    if failure:
        raise failure
    return value, generation
