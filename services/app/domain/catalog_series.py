"""Durable, account-scoped series catalogs; observation never authorizes downloads."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text

from app.adapters.catalog_providers import Hardcover, identifier
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.hardcover_series import advance, page
from app.config import get_settings
from app.db.models import CatalogAccount, CatalogSeries, Operation, SeriesMembership, User
from app.db.session import session_factory
from app.domain.catalog_network import CatalogGateway
from app.domain.hardcover_subscriptions import catalog_match
from app.domain.operations import transaction_lock
from app.domain.work_graph import graph_lock
from app.jobs.queue import enqueue
from app.jobs.retry import CatalogRetry
from app.security import decrypt_secrets

KIND = "catalog.series.refresh"


async def start(db, user, external_id, key, *, reuse_active=False):
    identifier("hardcover", external_id)
    if get_settings().recovery_mode:
        raise HTTPException(409, "Series refresh is paused for recovery")
    await transaction_lock(db, f"operation:{user.id}:{key}")
    command = {"provider": "hardcover", "external_id": external_id}
    old = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if old:
        if old.kind != KIND or old.payload.get("command") != command:
            raise HTTPException(409, "This operation key was used for a different request")
        return old
    account = await db.get(CatalogAccount, user.id)
    if not account or not account.enabled:
        raise HTTPException(409, "Connect your Hardcover account in Metadata settings first")
    await transaction_lock(db, f"series-catalog:{user.id}:{external_id}")
    row = await db.scalar(
        select(CatalogSeries)
        .where(
            CatalogSeries.owner_id == user.id,
            CatalogSeries.provider == "hardcover",
            CatalogSeries.external_id == external_id,
        )
        .execution_options(populate_existing=True)
    )
    if not row:
        row = CatalogSeries(
            owner_id=user.id,
            provider="hardcover",
            external_id=external_id,
            name="Series awaiting catalog",
            snapshot={},
        )
        db.add(row)
        await db.flush()
    if row.operation_id:
        previous = await db.get(Operation, row.operation_id)
        if previous and previous.status in {"queued", "running", "retrying"}:
            if (
                reuse_active
                and previous.payload.get("account_generation") == account.generation
                and previous.payload.get("endpoint") == get_settings().hardcover_url
                and previous.created_at >= datetime.now(UTC) - timedelta(minutes=30)
                and (await operation_status(db, previous))[0] != "interrupted"
            ):
                return previous
            previous.status, previous.message = "cancelled", "Superseded by a new series refresh"
    operation = Operation(
        owner_id=user.id,
        kind=KIND,
        idempotency_key=key,
        payload={
            "command": command,
            "series_id": str(row.id),
            "account_generation": account.generation,
            "endpoint": get_settings().hardcover_url,
        },
        message="Waiting to read the series catalog",
    )
    db.add(operation)
    await db.flush()
    row.operation_id = operation.id
    operation.job_id = await enqueue(db, KIND, operation_id=str(operation.id))
    return operation


async def context(db, operation_id):
    operation = await db.get(Operation, operation_id)
    if (
        not operation
        or operation.kind != KIND
        or operation.status in {"completed", "failed", "cancelled"}
    ):
        return None
    row = await db.get(CatalogSeries, UUID(operation.payload["series_id"]))
    if row:
        await transaction_lock(db, f"series-catalog:{row.owner_id}:{row.external_id}")
        await db.refresh(row)
    operation = await db.get(Operation, operation_id, with_for_update=True, populate_existing=True)
    if operation.status in {"completed", "failed", "cancelled"}:
        return None
    owner = await db.get(
        User, operation.owner_id, with_for_update={"read": True}, populate_existing=True
    )
    account = await db.get(
        CatalogAccount, operation.owner_id, with_for_update={"read": True}, populate_existing=True
    )
    if (
        not row
        or row.operation_id != operation.id
        or not owner
        or not owner.active
        or owner.role == "viewer"
        or not account
        or not account.enabled
        or account.generation != operation.payload["account_generation"]
        or get_settings().hardcover_url != operation.payload["endpoint"]
    ):
        operation.status, operation.message = (
            "cancelled",
            "Series access or catalog connection changed; refresh again",
        )
        return None
    if operation.created_at < datetime.now(UTC) - timedelta(minutes=30):
        operation.status, operation.message = "failed", "Series observation expired; refresh again"
        return None
    return operation, row, owner, account


async def fetch_page(owner_id, generation, token, external_id, cursor):
    try:
        async with asyncio.timeout(45):
            async with CatalogGateway(
                "hardcover", f"{owner_id}:{generation}", token, cache=False, request_interval=3.0
            ) as gateway:
                return await page(Hardcover(gateway.request).query, external_id, cursor)
    except TimeoutError:
        raise AdapterError(
            FailureKind.TIMEOUT, "Series page timed out; the previous catalog is preserved"
        ) from None


async def run(operation_id):
    if get_settings().recovery_mode:
        raise CatalogRetry(60)
    lease = str(uuid4())
    async with session_factory()() as db, db.begin():
        ctx = await context(db, operation_id)
        if not ctx:
            return
        operation, row, owner, account = ctx
        saved = operation.payload
        if saved.get("lease_until") and datetime.fromisoformat(saved["lease_until"]) > datetime.now(
            UTC
        ):
            raise CatalogRetry(60)
        stage = saved.get("stage")
        external_id, owner_id, generation = row.external_id, owner.id, account.generation
        token = decrypt_secrets(account.encrypted_token)["token"]
        operation.payload = {
            **saved,
            "lease": lease,
            "lease_until": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        }
        operation.status, operation.message = "running", "Reading and verifying series membership"
    try:
        observed = await fetch_page(
            owner_id, generation, token, external_id, stage["cursor"] if stage else 0
        )
        stage, complete = advance(stage, observed)
    except AdapterError as error:
        retry = error.kind in {FailureKind.RATE_LIMIT, FailureKind.TIMEOUT, FailureKind.UNAVAILABLE}
        async with session_factory()() as db, db.begin():
            ctx = await context(db, operation_id)
            if not ctx or ctx[0].payload.get("lease") != lease:
                return
            operation = ctx[0]
            failures = operation.payload.get("failures", 0) + 1
            retry = retry and failures < 5
            operation.payload = {
                **operation.payload,
                "lease_until": None,
                "failures": failures,
                "retry_at": (
                    datetime.now(UTC) + timedelta(seconds=max(error.retry_after or 60, 60))
                ).isoformat()
                if retry
                else None,
            }
            operation.status, operation.message = ("retrying" if retry else "failed"), str(error)
        if retry:
            raise CatalogRetry(error.retry_after) from None
        return
    async with session_factory()() as db, db.begin():
        ctx = await context(db, operation_id)
        if not ctx or ctx[0].payload.get("lease") != lease:
            return
        operation, row, owner, _ = ctx
        operation.payload = {**operation.payload, "stage": stage, "lease_until": None}
        if not complete:
            operation.status = "queued"
            operation.message = (
                f"Series {stage['phase']}: {len(stage['items'])} entries, "
                f"{stage['verified']} verified"
            )
            operation.job_id = await enqueue(db, KIND, operation_id=str(operation.id))
            return
        await graph_lock(db)
        await transaction_lock(db, f"goodreads:catalog:{owner.id}")
        members = {
            entry.external_id: entry
            for entry in await db.scalars(
                select(SeriesMembership).where(SeriesMembership.series_id == row.id)
            )
        }
        for member in members.values():
            member.present = False
        for record in stage["items"]:
            book = record["book"]
            work = await catalog_match(db, owner, {**book, "isbn": None, "isbn13": None})
            member = members.get(record["entry_id"])
            if not member:
                member = SeriesMembership(
                    series_id=row.id,
                    external_id=record["entry_id"],
                    work_id=work.id,
                    snapshot=record,
                )
                db.add(member)
            member.work_id, member.snapshot, member.present = work.id, record, True
        row.name, row.snapshot = stage["info"]["name"], stage["info"]
        row.fetched_at, row.generation = datetime.now(UTC), row.generation + 1
        operation.payload = {
            key: value
            for key, value in operation.payload.items()
            if key not in {"stage", "lease", "lease_until"}
        }
        operation.status, operation.message = (
            "completed",
            f"Verified {len(stage['items'])} series entries",
        )
        from app.domain.series_gap_watch import record_sightings

        await record_sightings(db, owner, row)


async def operation_status(db, operation):
    if operation and operation.status in {"queued", "running", "retrying"}:
        job = await db.scalar(
            text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
            {"id": operation.job_id},
        )
        if job not in {"todo", "doing"}:
            return (
                "interrupted",
                "Series refresh stopped; refresh again to resume with a new observation",
            )
    return (
        (operation.status, operation.message)
        if operation
        else ("not-loaded", "Load the series catalog")
    )
