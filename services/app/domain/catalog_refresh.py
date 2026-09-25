"""Durable, coalesced refreshes for display metadata. No credentials in job arguments."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException
from pydantic_core import to_jsonable_python
from sqlalchemy import select, text

from app.adapters.contracts import AdapterError
from app.config import get_settings
from app.db.models import CatalogAccount, Operation, RestoreCheckpoint, User
from app.db.session import session_factory
from app.domain.cache_entries import TRANSIENT
from app.domain.catalog_cache_policy import BROWSE_OPERATIONS
from app.domain.operations import transaction_lock
from app.jobs.queue import enqueue
from app.jobs.retry import CatalogRetry


async def schedule(user_id, provider, generation, operation, args):
    if get_settings().recovery_mode or operation not in BROWSE_OPERATIONS:
        return False
    endpoint = (
        get_settings().hardcover_url if provider == "hardcover" else get_settings().openlibrary_url
    )
    payload = to_jsonable_python(
        dict(
            provider=provider,
            generation=generation,
            operation=operation,
            args=args,
            endpoint=endpoint,
        )
    )
    async with session_factory()() as db, db.begin():
        # A post-restore refresh must not reuse an operation sealed in the restored queue.
        epoch = await db.scalar(
            select(RestoreCheckpoint.id)
            .order_by(RestoreCheckpoint.created_at.desc(), RestoreCheckpoint.id)
            .limit(1)
        )
        key = (
            "catalog-refresh:"
            + hashlib.sha256(json.dumps([payload, str(epoch)], sort_keys=True).encode()).hexdigest()
        )
        await transaction_lock(db, f"{user_id}:{key}")
        row = await db.scalar(
            select(Operation).where(Operation.owner_id == user_id, Operation.idempotency_key == key)
        )
        if row:
            status = await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": row.job_id},
            )
            if status in {"todo", "doing"} or row.updated_at > datetime.now(UTC) - timedelta(
                minutes=5
            ):
                return True
        else:
            row = Operation(
                owner_id=user_id, kind="catalog.refresh", idempotency_key=key, payload=payload
            )
            db.add(row)
            await db.flush()
        row.status, row.message = "queued", "Refreshing cached catalog data"
        row.job_id = await enqueue(
            db, "catalog.refresh", operation_id=str(row.id), job_lock=f"catalog-refresh:{row.id}"
        )
    return True


async def run(operation_id):
    from app.api.metadata import provider_call

    async with session_factory()() as db, db.begin():
        row = await db.get(Operation, operation_id, with_for_update=True)
        if not row or row.kind != "catalog.refresh" or row.status in {"completed", "cancelled"}:
            return
        payload, owner_id = dict(row.payload), row.owner_id
        user = await db.get(User, owner_id)
        account = await db.get(CatalogAccount, owner_id)
        provider = payload["provider"]
        endpoint = (
            get_settings().hardcover_url
            if provider == "hardcover"
            else get_settings().openlibrary_url
        )
        if (
            get_settings().recovery_mode
            or not user
            or not user.active
            or payload["operation"] not in BROWSE_OPERATIONS
            or provider not in {"hardcover", "openlibrary"}
            or endpoint != payload["endpoint"]
            or (
                provider == "hardcover"
                and (
                    not account
                    or not account.enabled
                    or account.generation != payload["generation"]
                )
            )
        ):
            row.status, row.message = "cancelled", "Catalog access or connection changed"
            return
        row.status = "running"
    args = list(payload["args"])
    if payload["operation"] == "discovery":
        args[2] = date.fromisoformat(args[2])
    elif payload["operation"] == "upcoming":
        args[:2] = [date.fromisoformat(value) for value in args[:2]]
    failure = None
    try:
        async with session_factory()() as db:
            # A different request may already have filled some constituent queries.
            _, stale, _ = await provider_call(
                db,
                owner_id,
                provider,
                payload["operation"],
                *args,
                expected_generation=payload["generation"],
            )
            if stale:
                raise CatalogRetry(60)
    except Exception as error:
        failure = error
    async with session_factory()() as db, db.begin():
        row = await db.get(Operation, operation_id)
        row.status = "failed" if failure else "completed"
        row.message = (
            "Catalog refresh will retry when available"
            if failure
            else "Cached catalog data refreshed"
        )
    if isinstance(failure, CatalogRetry):
        raise failure
    if isinstance(failure, AdapterError) and failure.kind in TRANSIENT:
        raise CatalogRetry(failure.retry_after) from failure
    if failure and not isinstance(failure, (AdapterError, HTTPException)):
        raise failure
