"""Checkpointed Hardcover pagination; publish only after a matching second pass."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.adapters.catalog_providers import Hardcover
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.hardcover_lists import MAX_MEMBERS, invalid
from app.config import get_settings
from app.db.models import (
    CatalogAccount,
    ListCatalogBinding,
    ListObservation,
    Work,
    WorkMetadataSource,
)
from app.db.session import session_factory
from app.domain.catalog_network import CatalogGateway
from app.domain.list_subscriptions import apply_records, context, next_due
from app.domain.visibility import visible_origin_work, visible_work
from app.domain.work_graph import canonical_work
from app.jobs.retry import ShelfRetry
from app.security import decrypt_secrets, encrypt_secrets


async def fetch_page(owner_id, generation, token, external_id, cursor, *, follow=None):
    try:
        async with asyncio.timeout(45):
            async with CatalogGateway(
                "hardcover", f"{owner_id}:{generation}", token, cache=False
            ) as gateway:
                provider = Hardcover(gateway.request)
                if follow:
                    from app.adapters.hardcover_follows import FollowFilters, page

                    return await page(
                        provider.query,
                        follow["source_kind"],
                        external_id,
                        cursor,
                        FollowFilters.model_validate(follow.get("filters", {})),
                    )
                return await provider.list_page(external_id, cursor)
    except TimeoutError:
        raise AdapterError(
            FailureKind.TIMEOUT, "Hardcover list page timed out; previous memberships are preserved"
        ) from None


def advance(stage, page):
    if not stage:
        stage = {
            "info": page.info,
            "items": [],
            "cursor": 0,
            "phase": "collect",
            "verified": 0,
            "started_at": datetime.now(UTC).isoformat(),
        }
    if page.info != stage["info"]:
        raise invalid()
    if stage["phase"] == "collect":
        stage["items"] = [*stage["items"], *page.items]
        if len(stage["items"]) > MAX_MEMBERS or len(stage["items"]) > page.info["count"]:
            raise invalid()
        if page.items:
            stage["cursor"] = page.cursor
        else:
            if len(stage["items"]) != page.info["count"]:
                raise invalid()
            stage["phase"], stage["cursor"] = "verify", 0
        return stage, False
    offset = stage["verified"]
    if page.items != stage["items"][offset : offset + len(page.items)]:
        raise invalid()
    if not page.items:
        if offset != len(stage["items"]):
            raise invalid()
        return stage, True
    stage["verified"] = offset + len(page.items)
    stage["cursor"] = page.cursor
    return stage, False


def books(stage):
    records = {}
    for entry in stage["items"]:
        key = entry["external_id"]
        metadata = {k: entry[k] for k in ("external_id", "title", "authors", "isbn", "isbn13")}
        if key in records and any(records[key][k] != metadata[k] for k in metadata):
            raise invalid()
        if key not in records:
            records[key] = {**metadata, "memberships": []}
        records[key]["memberships"].append(
            {k: entry[k] for k in ("entry_id", "edition_id", "position", "date_added")}
        )
    return list(records.values())


async def catalog_match(db, owner, record):
    key = f"hardcover:{record['external_id']}"
    binding = await db.scalar(
        select(ListCatalogBinding).where(
            ListCatalogBinding.owner_id == owner.id, ListCatalogBinding.identity_key == key
        )
    )
    if binding:
        work = await canonical_work(db, binding.work_id)
        if await db.scalar(select(Work.id).where(Work.id == work.id, visible_work(owner))):
            return work
    sources = (
        await db.scalars(
            select(Work)
            .join(WorkMetadataSource)
            .where(
                WorkMetadataSource.provider == "hardcover",
                WorkMetadataSource.external_id == record["external_id"],
                WorkMetadataSource.accepted.is_(True),
                visible_origin_work(owner),
            )
        )
    ).all()
    roots = {work.id: work for work in [await canonical_work(db, w.id) for w in sources]}
    if len(roots) == 1:
        work = next(iter(roots.values()))
    else:
        work = Work(
            title=record["title"],
            authors=record["authors"],
            provisional=True,
            catalog_public=False,
            catalog_owner_id=owner.id,
        )
        db.add(work)
        await db.flush()
    if not binding:
        db.add(
            ListCatalogBinding(
                owner_id=owner.id,
                identity_key=key,
                work_id=work.id,
                assertion={k: record[k] for k in ("title", "authors", "isbn", "isbn13")},
            )
        )
    return work


async def run(operation_id):
    if get_settings().recovery_mode:
        raise ShelfRetry(60)
    token = uuid4()
    async with session_factory()() as db, db.begin():
        ctx = await context(db, operation_id)
        if not ctx:
            return
        operation, row, owner = ctx
        if operation.status in {"completed", "failed"}:
            return
        now = datetime.now(UTC)
        if operation.created_at < now - timedelta(days=7):
            operation.status, row.state = "failed", "failed"
            row.message = operation.message = "Hardcover observation expired; start a new refresh"
            row.next_sync_at = next_due(row, now, failed=True)
            row.run_token = None
            return
        if row.run_token and row.lease_until > now:
            raise ShelfRetry((row.lease_until - now).total_seconds() + 1)
        stage = operation.payload.get("stage")
        if stage and datetime.fromisoformat(stage["started_at"]) < now - timedelta(minutes=15):
            stage = None
        config = decrypt_secrets(row.encrypted_config)
        account = await db.get(CatalogAccount, owner.id)
        secret = decrypt_secrets(account.encrypted_token)["token"]
        generation = account.generation
        owner_id = owner.id
        row.run_token, row.lease_until = token, now + timedelta(minutes=2)
        row.state, operation.status = "running", "running"
        row.message = operation.message = (
            "Reading Hardcover list pages"
            if not stage or stage["phase"] == "collect"
            else "Verifying Hardcover list membership"
        )
    delay = 0
    try:
        page = await fetch_page(
            owner_id,
            generation,
            secret,
            config["external_id"],
            stage["cursor"] if stage else 0,
            **({"follow": config} if config.get("source_kind") else {}),
        )
        stage, complete = advance(stage, page)
        records = (
            (stage["items"] if config.get("source_kind") else books(stage)) if complete else []
        )
    except AdapterError as error:
        async with session_factory()() as db, db.begin():
            ctx = await context(db, operation_id)
            if not ctx or ctx[1].run_token != token:
                return
            operation, row, _ = ctx
            row.run_token = None
            row.message = operation.message = str(error)
            if error.kind == FailureKind.RATE_LIMIT:
                delay = max(1, error.retry_after or 60)
                row.state, operation.status = "queued", "queued"
            else:
                row.state, operation.status = "failed", "failed"
                row.failures += 1
                row.next_sync_at = next_due(
                    row, datetime.now(UTC), failed=True, retry_after=error.retry_after or 0
                )
        if delay:
            raise ShelfRetry(delay) from None
        return
    async with session_factory()() as db, db.begin():
        ctx = await context(db, operation_id)
        if not ctx or ctx[1].run_token != token:
            return
        operation, row, owner = ctx
        row.run_token = None
        if not complete:
            operation.payload = {**operation.payload, "stage": stage}
            operation.status, row.state = "queued", "queued"
            row.message = operation.message = (
                f"Hardcover {stage['phase']}: {len(stage['items'])} memberships staged, "
                f"{stage['verified']} verified"
            )
        else:
            from app.api.list_subscriptions import remove_unneeded

            previous = set(
                await db.scalars(
                    select(ListObservation.external_id).where(
                        ListObservation.subscription_id == row.id
                    )
                )
            )
            had_baseline = row.baseline_at is not None
            added = await apply_records(db, row, owner, records)
            present = {r["external_id"] for r in records}
            removed = []
            for observation in await db.scalars(
                select(ListObservation).where(
                    ListObservation.subscription_id == row.id, ListObservation.present.is_(True)
                )
            ):
                if observation.external_id not in present:
                    observation.present = False
                    removed.append(observation.work_id)
            await db.flush()
            for work_id in set(removed):
                await remove_unneeded(db, owner, row, work_id)
            if config.get("source_kind"):
                from app.domain.follows import publish_metadata, record_discoveries

                await publish_metadata(db, row, owner)
                if had_baseline:
                    await record_discoveries(db, row, owner, config, previous)
            now = datetime.now(UTC)
            row.encrypted_config = encrypt_secrets(
                {
                    **config,
                    "name": stage["info"]["name"],
                    "complete": True,
                    "last_count": len(records),
                }
            )
            row.baseline_at = row.baseline_at or now
            row.last_success_at, row.next_sync_at = now, next_due(row, now)
            row.state, row.failures = "idle", 0
            operation.payload = {k: v for k, v in operation.payload.items() if k != "stage"}
            row.message = operation.message = (
                f"Hardcover list verified: {len(records)} books; {added} new, "
                f"{len(removed)} no longer listed"
            )
            operation.status = "completed"
    if not complete:
        raise ShelfRetry(1)
