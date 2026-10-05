import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, func, select, text

from app.db.models import (
    BookList,
    CatalogAccount,
    ListAcquisitionPolicy,
    ListSubscription,
    Operation,
    User,
)
from app.domain import list_automation, list_subscriptions
from app.jobs.queue import enqueue

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("busy", [False, True])
async def test_policy_scheduler_reaches_eligible_work_without_loading_snapshots(
    client, admin, database, tmp_path, busy
):
    now = datetime.now(UTC)
    payload = {"snapshot": "x" * 100_000}
    old_ids, eligible_ids = [], []
    async with database() as db, db.begin():
        for n in range(21 if busy else 20):
            item = BookList(owner_id=UUID(admin["id"]), name=f"Policy {n}")
            db.add(item)
            await db.flush()
            operation = Operation(
                owner_id=item.owner_id,
                kind=list_automation.KIND,
                idempotency_key=f"old-policy-{n}",
                payload=payload,
                status="running" if n % 2 else "queued",
            )
            db.add(operation)
            await db.flush()
            operation.job_id = await enqueue(
                db, list_automation.KIND, operation_id=str(operation.id)
            )
            live = busy and n < 20
            await db.execute(
                text("UPDATE book_queue.procrastinate_jobs SET status=:status WHERE id=:id"),
                {"id": operation.job_id, "status": "doing" if live else "failed"},
            )
            policy = ListAcquisitionPolicy(
                list_id=item.id,
                owner_id=item.owner_id,
                configuration={"mode": "automatic", **payload},
                baseline_at=now,
                next_check_at=now - timedelta(minutes=30 - n),
                message="Ready",
                operation_id=operation.id,
            )
            db.add(policy)
            await db.flush()
            old_ids.append(operation.id)
            if not live:
                eligible_ids.append(policy.id)

    calls, loaded = [], []
    engine = database.kw["bind"].sync_engine

    def count(*args):
        calls.append(1)

    def capture(row, context):
        loaded.append({k: v for k, v in vars(row).items() if not k.startswith("_")})

    event.listen(engine, "before_cursor_execute", count)
    for model in (ListAcquisitionPolicy, Operation):
        event.listen(model, "load", capture)
    started = time.perf_counter()
    try:
        await list_automation.schedule()
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", count)
        for model in (ListAcquisitionPolicy, Operation):
            event.remove(model, "load", capture)
    metrics = {
        "queries": len(calls),
        "loaded_bytes": len(json.dumps(loaded, default=str)),
        "elapsed_ms": elapsed * 1000,
    }
    (tmp_path / "policy-metrics.json").write_text(json.dumps(metrics, indent=2))
    async with database() as db:
        for row in await db.scalars(select(ListAcquisitionPolicy)):
            if row.id in eligible_ids:
                assert row.operation_id not in old_ids
                assert row.next_check_at > now
            else:
                assert row.operation_id in old_ids
            assert row.configuration["snapshot"] == payload["snapshot"]
        for row in await db.scalars(select(Operation).where(Operation.id.in_(old_ids))):
            assert row.payload == payload
        assert await db.scalar(
            select(func.count()).select_from(Operation).where(Operation.status == "failed")
        ) == len(eligible_ids)
    # Queue inserts use the shared raw psycopg connection; this count covers
    # ORM reads/writes, which should be batched independently of policy count.
    assert metrics["queries"] <= 8
    assert metrics["loaded_bytes"] < 30_000

    # Two ticks racing on the same eligible rows must enqueue only one new job per policy.
    async with database() as db, db.begin():
        for row in await db.scalars(
            select(ListAcquisitionPolicy).where(ListAcquisitionPolicy.id.in_(eligible_ids))
        ):
            row.next_check_at = now - timedelta(minutes=1)
            operation = await db.get(Operation, row.operation_id)
            await db.execute(
                text("UPDATE book_queue.procrastinate_jobs SET status='succeeded' WHERE id=:id"),
                {"id": operation.job_id},
            )
    await asyncio.gather(list_automation.schedule(), list_automation.schedule())
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == (
            len(old_ids) + 2 * len(eligible_ids)
        )


@pytest.mark.parametrize(
    "blocker",
    ["inactive", "viewer", "busy", "busy-hardcover", "stopped", "account", "disabled-account"],
)
async def test_shelf_scheduler_reaches_eligible_work_and_preserves_recovery(
    client, admin, database, blocker
):
    now = datetime.now(UTC)
    async with database() as db, db.begin():
        owner = User(
            username="shelf-owner",
            display_name="Shelf owner",
            password_hash="unused",
            active=blocker != "inactive",
            role="viewer" if blocker == "viewer" else "member",
        )
        db.add(owner)
        await db.flush()
        if blocker in {"busy-hardcover", "disabled-account"}:
            db.add(
                CatalogAccount(
                    user_id=owner.id, encrypted_token="unused", enabled=blocker == "busy-hardcover"
                )
            )
        rows = []
        for n in range(51):
            item = BookList(owner_id=owner.id if n < 50 else UUID(admin["id"]), name=f"Shelf {n}")
            db.add(item)
            await db.flush()
            row = ListSubscription(
                list_id=item.id,
                encrypted_config="unused",
                provider="hardcover"
                if blocker in {"account", "disabled-account", "busy-hardcover"} and n < 50
                else "goodreads",
                next_sync_at=now - timedelta(minutes=60 - n),
            )
            db.add(row)
            if n < 50 and blocker not in {"inactive", "viewer"}:
                operation = Operation(
                    owner_id=item.owner_id,
                    kind="lists.sync",
                    idempotency_key=f"shelf-{n}",
                    payload={"list_id": str(item.id)},
                )
                db.add(operation)
                await db.flush()
                operation.job_id = await enqueue(db, "lists.sync", operation_id=str(operation.id))
                await db.execute(
                    text("UPDATE book_queue.procrastinate_jobs SET status=:status WHERE id=:id"),
                    {
                        "id": operation.job_id,
                        "status": "failed" if blocker == "stopped" else "doing",
                    },
                )
                row.state, row.operation_id = "running", operation.id
            await db.flush()
            rows.append(row.id)
    # Stopped jobs and disabled accounts consume a bounded repair batch first;
    # live jobs and ineligible owners should not consume either tick's capacity.
    await asyncio.gather(list_subscriptions.schedule(), list_subscriptions.schedule())
    await list_subscriptions.schedule()
    async with database() as db:
        eligible = await db.get(ListSubscription, rows[-1])
        assert eligible.state == "queued"
        assert eligible.operation_id
        for row in await db.scalars(
            select(ListSubscription).where(ListSubscription.id.in_(rows[:-1]))
        ):
            if blocker in {"stopped", "account", "disabled-account"}:
                assert row.state == "failed" and row.next_sync_at > now
                assert (await db.get(Operation, row.operation_id)).status == "failed"
            elif blocker in {"busy", "busy-hardcover"}:
                assert row.state == "running"
            else:
                assert row.operation_id is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.owner_id == UUID(admin["id"]))
            )
            == 1
        )
