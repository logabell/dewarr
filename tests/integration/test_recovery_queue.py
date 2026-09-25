from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.db.models import (
    AuditEvent,
    Operation,
    RecoveryQueueFence,
    RecoveryQueueSubject,
    RestoreCheckpoint,
)
from app.jobs.queue import enqueue, get_queue
from app.recovery_queue import seal
from tests.integration.test_recovery_scan_workflow import pause

pytestmark = pytest.mark.integration


async def probe(client, key):
    result = await client.post("/api/system/probe", headers={"Idempotency-Key": key})
    assert result.status_code == 202, result.text
    return UUID(result.json()["id"])


async def drain():
    await get_queue().run_worker_async(wait=False, concurrency=1)


async def close_fixture(database, checkpoint):
    # Test the durable boundary after the active flag is cleared. This is not a resume API.
    async with database() as db, db.begin():
        (await db.get(RestoreCheckpoint, checkpoint)).active = False


async def test_old_job_and_requeued_old_operation_are_both_fenced(client, admin, database):
    old = await probe(client, "before-restore")
    checkpoint = await pause(database, admin)
    async with database() as db, db.begin():
        await seal(db, checkpoint)
        fence = await db.get(RecoveryQueueFence, checkpoint)
        ceiling = fence.job_id_through
        assert fence.job_count == 1 and fence.subject_counts == {"operation": 1}
    await close_fixture(database, checkpoint)
    await drain()
    async with database() as db, db.begin():
        saved = await db.get(Operation, old)
        assert saved.status == "queued"
        assert (
            await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": saved.job_id},
            )
            == "aborted"
        )
        clone = await enqueue(db, "system.probe", operation_id=str(old))
        assert clone > ceiling
    await drain()
    async with database() as db:
        assert (await db.get(Operation, old)).status == "queued"
        assert (
            await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": clone},
            )
            == "aborted"
        )
        events = list(
            await db.scalars(
                select(AuditEvent).where(AuditEvent.action == "recovery.queue.blocked")
            )
        )
        assert len(events) == 2 and all("operation_id" not in event.detail for event in events)
    fresh = await probe(client, "after-restore")
    await drain()
    async with database() as db:
        assert (await db.get(Operation, fresh)).status == "completed"


async def test_boundary_is_immutable_and_survives_operation_deletion(client, admin, database):
    old = await probe(client, "before-boundary")
    checkpoint = await pause(database, admin)
    async with database() as db, db.begin():
        await seal(db, checkpoint)
        ceiling = (await db.get(RecoveryQueueFence, checkpoint)).job_id_through
        added = Operation(
            owner_id=UUID(admin["id"]), kind="system.probe", idempotency_key="after-seal"
        )
        db.add(added)
        await db.flush()
        newer = await enqueue(db, "system.probe", operation_id=str(added.id))
        await seal(db, checkpoint)
        assert (await db.get(RecoveryQueueFence, checkpoint)).job_id_through == ceiling
        assert newer > ceiling
        await db.delete(await db.get(Operation, old))
    async with database() as db:
        assert await db.get(RecoveryQueueSubject, (checkpoint, "operation", old))
        assert not await db.get(RecoveryQueueSubject, (checkpoint, "operation", added.id))


@pytest.mark.parametrize(
    "argument,kind,task",
    [
        ("operation_id", "operation", "system.probe"),
        ("search_id", "operation", "sources.prepare"),
        ("attempt_id", "download-attempt", "acquisition.download"),
        ("automatic_id", "automatic-import", "organization.automatic"),
        ("continuation_id", "import-continuation", "organization.reuse"),
        ("work_id", "work", "acquisition.fulfillment"),
    ],
)
async def test_every_record_addressed_entrypoint_honors_the_subject_fence(
    client, admin, database, argument, kind, task, monkeypatch
):
    checkpoint = await pause(database, admin)
    identifier = uuid4()
    async with database() as db, db.begin():
        await seal(db, checkpoint)
        db.add(RecoveryQueueSubject(checkpoint_id=checkpoint, kind=kind, subject_id=identifier))
        job = await enqueue(db, task, **{argument: str(identifier)})
    await close_fixture(database, checkpoint)
    called = []

    async def forbidden(**kwargs):
        called.append(kwargs)

    monkeypatch.setattr(get_queue().tasks[task], "func", forbidden)
    await drain()
    assert not called
    async with database() as db:
        assert (
            await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": job},
            )
            == "aborted"
        )


async def test_confirmation_batch_checks_every_operation_against_restore_fence(
    client, admin, database, monkeypatch
):
    checkpoint = await pause(database, admin)
    restored = uuid4()
    async with database() as db, db.begin():
        await seal(db, checkpoint)
        db.add(
            RecoveryQueueSubject(checkpoint_id=checkpoint, kind="operation", subject_id=restored)
        )
        job = await enqueue(
            db, "organization.confirm-batch", operation_ids=[str(uuid4()), str(restored)]
        )
    await close_fixture(database, checkpoint)
    called = []

    async def forbidden(**kwargs):
        called.append(kwargs)

    monkeypatch.setattr(get_queue().tasks["organization.confirm-batch"], "func", forbidden)
    await drain()
    assert not called
    async with database() as db:
        assert (
            await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": job},
            )
            == "aborted"
        )


async def test_pause_blocks_fresh_ordinary_work_and_unsealed_history_never_allows_execution(
    client, admin, database
):
    checkpoint = await pause(database, admin)
    async with database() as db, db.begin():
        row = Operation(
            owner_id=UUID(admin["id"]), kind="system.probe", idempotency_key="while-paused"
        )
        db.add(row)
        await db.flush()
        row.job_id = await enqueue(db, "system.probe", operation_id=str(row.id))
        identifier = row.id
    await drain()
    await close_fixture(database, checkpoint)
    async with database() as db, db.begin():
        await enqueue(db, "system.probe", operation_id=str(identifier))
    await drain()
    async with database() as db:
        assert (await db.get(Operation, identifier)).status == "queued"
        reasons = [
            r.detail["reason"]
            for r in await db.scalars(
                select(AuditEvent).where(AuditEvent.action == "recovery.queue.blocked")
            )
        ]
        assert any("paused" in reason for reason in reasons)
        assert any("no sealed queue boundary" in reason for reason in reasons)


async def test_later_restore_adds_boundary_without_erasing_earlier_subjects(
    client, admin, database
):
    first = await probe(client, "first-generation")
    one = await pause(database, admin)
    async with database() as db, db.begin():
        await seal(db, one)
    await close_fixture(database, one)
    second = await probe(client, "second-generation")
    two = await pause(database, admin)
    async with database() as db, db.begin():
        await seal(db, two)
    await close_fixture(database, two)
    await drain()
    async with database() as db:
        assert (
            (await db.get(Operation, first)).status
            == (await db.get(Operation, second)).status
            == "queued"
        )
        assert await db.get(RecoveryQueueSubject, (one, "operation", first))
        assert await db.get(RecoveryQueueSubject, (two, "operation", second))


async def test_seal_rollback_cannot_leave_partial_boundary(client, admin, database):
    saved = await probe(client, "rollback-seal")
    checkpoint = await pause(database, admin)
    async with database() as db:
        await seal(db, checkpoint)
        await db.rollback()
    async with database() as db:
        assert not await db.get(RecoveryQueueFence, checkpoint)
        assert not await db.get(RecoveryQueueSubject, (checkpoint, "operation", saved))


async def test_upgrade_seals_existing_paused_restore_and_blocks_loss_of_boundary(
    client, admin, database
):
    import asyncio
    import subprocess

    saved = await probe(client, "migration-history")
    checkpoint = await pause(database, admin)

    async def migrate(*args):
        return await asyncio.to_thread(
            subprocess.run, ["uv", "run", "alembic", *args], capture_output=True, timeout=20
        )

    down = await migrate("downgrade", "0042_recovery_scans")
    assert down.returncode == 0, down.stderr.decode()
    up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr.decode()
    async with database() as db:
        fence = await db.get(RecoveryQueueFence, checkpoint)
        assert fence and fence.job_count == 1
        assert await db.get(RecoveryQueueSubject, (checkpoint, "operation", saved))
    async with database() as db:
        current_revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
    rejected = await migrate("downgrade", "0042_recovery_scans")
    assert rejected.returncode != 0 and b"Restored approval boundaries require" in rejected.stderr
    async with database() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == current_revision


async def test_cleanup_and_unknown_record_reference_cannot_erase_or_bypass_history(
    client, admin, database, monkeypatch
):
    checkpoint = await pause(database, admin)
    async with database() as db, db.begin():
        await seal(db, checkpoint)
        cleanup = await enqueue(db, "procrastinate.builtin_tasks.remove_old_jobs", max_hours=1)
        unknown = await enqueue(db, "system.probe", unknown_id=str(uuid4()))
    await close_fixture(database, checkpoint)
    await drain()
    async with database() as db:
        for job in (cleanup, unknown):
            assert (
                await db.scalar(
                    text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                    {"id": job},
                )
                == "aborted"
            )
        assert await db.get(RecoveryQueueFence, checkpoint)


async def test_discovery_refresh_is_held_after_restore(client, admin, database, monkeypatch):
    checkpoint = await pause(database, admin)
    async with database() as db, db.begin():
        await seal(db, checkpoint)
        job = await enqueue(
            db, "discovery.refresh", user_id=admin["id"], collection_id="goodreads:42", generation=1
        )
    await close_fixture(database, checkpoint)
    called = []

    async def forbidden(**kwargs):
        called.append(kwargs)

    monkeypatch.setattr(get_queue().tasks["discovery.refresh"], "func", forbidden)
    await drain()
    assert not called
    async with database() as db:
        assert (
            await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": job},
            )
            == "aborted"
        )
