# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.db.models import ImportCapacity, ImportEntry
from app.domain import capacity
from app.importing import cancellation, execution
from app.jobs.queue import get_queue
from app.jobs.tasks import schedule_import_confirmation
from tests.abs_import_fixture import ScanningBackend
from tests.integration.test_import_cancellation import cancel, state
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_destinations import start_probe
from tests.integration.test_import_execution import start

pytestmark = pytest.mark.integration
GIB = 1024**3


@pytest.fixture(params=["hardlink", "copy"])
async def capacity_route(request, client, destination_route, monkeypatch):
    route = destination_route
    if request.param == "copy":
        response = await client.put(
            "/api/organization/destinations/ebooks",
            json={
                "library_id": route["library_id"],
                "medium": "ebook",
                "backend_path": "/books",
                "mode": "copy",
                "expected_revision": route["destination"]["revision"],
            },
        )
        assert response.status_code == 200, response.text
        route["destination"] = response.json()
    await start_probe(client, route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    monkeypatch.setattr(execution, "Audiobookshelf", ScanningBackend(route["target"]).client)
    route["plan"] = (await client.get(f"/api/organization/plans/{route['plan_id']}")).json()
    return route


def space(monkeypatch, available):
    original = capacity.measure

    def measured(paths):
        observation = original(paths)
        for values in observation["filesystems"].values():
            values.update(available=available(), total=200 * GIB)
        return observation

    monkeypatch.setattr(capacity, "measure", measured)


async def test_low_space_waits_and_periodic_scheduler_resumes_same_import(
    client, database, capacity_route, monkeypatch
):
    free = GIB
    space(monkeypatch, lambda: free)
    run = (await start(client, capacity_route)).json()
    await get_queue().run_worker_async(wait=False, concurrency=1)
    current = await state(client, run)
    assert current["state"] == "queued" and "free disk space" in current["message"]
    assert not list(capacity_route["target"].rglob("*.epub"))
    async with database() as db, db.begin():
        entry = await db.get(ImportEntry, UUID(current["id"]))
        assert entry.next_check_at and not entry.published_at
        entry.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
    free = 100 * GIB
    await schedule_import_confirmation(0)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert (await state(client, run))["state"] == "confirmed"
    async with database() as db:
        assert not (await db.get(ImportCapacity, entry.id)).resources


async def test_confirmation_scheduler_skips_live_jobs_before_selecting_a_page(
    client, database, capacity_route
):
    from app.db.models import Operation
    from app.jobs.queue import enqueue

    run = (await start(client, capacity_route)).json()
    due = datetime.now(UTC) - timedelta(minutes=5)
    async with database() as db, db.begin():
        original = await db.get(ImportEntry, UUID(run["entries"][0]["id"]))
        owner_id = (await db.get(Operation, original.operation_id)).owner_id
        live_ids = []
        for n in range(22):
            operation = Operation(
                owner_id=owner_id,
                kind="organization.publish",
                idempotency_key=f"scheduler-busy-{n}",
            )
            db.add(operation)
            await db.flush()
            entry = ImportEntry(
                run_id=original.run_id,
                group_id=uuid4(),
                version_id=original.version_id,
                destination_id=original.destination_id,
                operation_id=operation.id,
                state="awaiting-library",
                message="Waiting for detection",
                next_check_at=due + timedelta(seconds=n),
            )
            db.add(entry)
            await db.flush()
            operation.payload = {"entry_id": str(entry.id)}
            if n < 21:
                operation.job_id = await enqueue(
                    db, "organization.publish", operation_id=str(operation.id)
                )
                live_ids.append(operation.job_id)
            else:
                waiting_id = operation.id
    await schedule_import_confirmation(0)
    async with database() as db:
        from sqlalchemy import text

        waiting = await db.get(Operation, waiting_id)
        assert waiting.job_id and waiting.job_id not in live_ids
        assert (
            await db.scalar(
                text("SELECT queue_name FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": waiting.job_id},
            )
            == "confirmation"
        )


async def test_free_space_floor_is_rechecked_after_staging_without_losing_prepared_files(
    client, database, capacity_route, monkeypatch
):
    free = 100 * GIB
    space(monkeypatch, lambda: free)
    run = (await start(client, capacity_route)).json()

    def depleted(phase):
        nonlocal free
        if phase == "prepared":
            free = GIB

    await execution.execute(UUID(run["entries"][0]["operation_id"]), checkpoint=depleted)
    assert (await state(client, run))["state"] == "queued"
    assert not list(capacity_route["target"].rglob("*.epub"))
    staged = next(capacity_route["stage"].rglob("*.epub"))
    inode, original = staged.stat().st_ino, staged.read_bytes()
    async with database() as db:
        claim = await db.get(ImportCapacity, UUID(run["entries"][0]["id"]))
        assert sum(claim.resources.values()) == capacity.MIB
    free = 10 * GIB + capacity.MIB  # Reserve floor plus rename allowance, no fresh copy.
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert (await state(client, run))["state"] == "confirmed"
    published = next(capacity_route["target"].rglob("*.epub"))
    assert published.stat().st_ino == inode and published.read_bytes() == original


async def test_crash_before_consumption_recovers_without_reserving_a_second_copy(
    client, database, capacity_route, monkeypatch
):
    run = (await start(client, capacity_route)).json()
    operation_id = UUID(run["entries"][0]["operation_id"])

    def crash(phase):
        if phase == "prepared":
            raise RuntimeError("Crash before capacity consumption")

    with pytest.raises(RuntimeError, match="capacity consumption"):
        await execution.execute(operation_id, checkpoint=crash)
    async with database() as db:
        claim = await db.get(ImportCapacity, UUID(run["entries"][0]["id"]))
        assert sum(claim.resources.values()) > capacity.MIB
    inode = next(capacity_route["stage"].rglob("*.epub")).stat().st_ino
    space(monkeypatch, lambda: 10 * GIB + capacity.MIB)
    await execution.execute(operation_id)
    assert (await state(client, run))["state"] == "confirmed"
    assert next(capacity_route["target"].rglob("*.epub")).stat().st_ino == inode


async def test_cancel_releases_reservation_only_after_durable_cleanup_acknowledgement(
    client, database, capacity_route
):
    run = (await start(client, capacity_route)).json()
    entry_id, operation_id = (UUID(run["entries"][0][name]) for name in ("id", "operation_id"))

    def crash(phase):
        if phase in {"prepared", "cancel-before-database"}:
            raise RuntimeError("Crash before acknowledgement")

    with pytest.raises(RuntimeError):
        await execution.execute(operation_id, checkpoint=crash)
    assert (await cancel(client, run)).status_code == 202
    with pytest.raises(RuntimeError):
        await cancellation.execute(operation_id, checkpoint=crash)
    async with database() as db:
        assert (await db.get(ImportCapacity, entry_id)).resources
    assert not list(capacity_route["stage"].glob("item-*"))
    await cancellation.execute(operation_id)
    assert (await state(client, run))["state"] == "cancelled"
    async with database() as db:
        assert not (await db.get(ImportCapacity, entry_id)).resources
    assert (capacity_route["source"] / "pack/book.epub").is_file()
    assert not list(capacity_route["target"].rglob("*.epub"))


async def test_two_recovered_copies_consume_claims_even_when_peer_prevents_admission(
    client, database, capacity_route
):
    run = (await start(client, capacity_route)).json()
    first, second = UUID(run["entries"][0]["id"]), uuid4()
    observation = {
        "at": datetime.now(UTC).isoformat(),
        "roots": {"library": "disk", "staging": "disk"},
        "filesystems": {"disk": {"available": 10 * GIB + 2 * capacity.MIB, "total": 200 * GIB}},
        "required_bytes": capacity.MIB,
    }
    async with database() as db, db.begin():
        entry = await db.get(ImportEntry, first)
        db.add(
            ImportEntry(
                id=second,
                run_id=entry.run_id,
                group_id=uuid4(),
                version_id=entry.version_id,
                message="Separate synthetic staged capacity claim",
                reserved=False,
            )
        )
        await db.flush()
        for identifier in (first, second):
            db.add(
                ImportCapacity(
                    entry_id=identifier,
                    resources={"disk": 6 * GIB},
                    observed_mounts=observation["roots"],
                )
            )
    # Each journal reports a verified complete stage after a restart. Even if
    # admission waits, persist the physical consumption to prevent mutual waits.
    async with database() as db, db.begin():
        await capacity.reconcile_import(db, await db.get(ImportEntry, first), observation)
    with pytest.raises(capacity.CapacityWait, match="free disk space"):
        async with database() as db, db.begin():
            observation["generation"] = await capacity.storage_generation(db)
            await capacity.publication_capacity(db, await db.get(ImportEntry, first), observation)
    async with database() as db, db.begin():
        await capacity.reconcile_import(db, await db.get(ImportEntry, second), observation)
    for identifier in (first, second):
        async with database() as db, db.begin():
            observation["generation"] = await capacity.storage_generation(db)
            await capacity.publication_capacity(
                db, await db.get(ImportEntry, identifier), observation
            )


async def test_migration_retains_unknown_import_cost_until_existing_entry_reconciles(
    client, database, capacity_route
):
    from app.db.session import get_engine
    from tests.integration.test_correction_migration import migrate

    run = (await start(client, capacity_route)).json()
    entry_id = UUID(run["entries"][0]["id"])
    await get_engine().dispose()
    try:
        previous = await migrate("downgrade", "0027_hardcover_lists")
        assert previous.returncode == 0, previous.stderr
        assert (await migrate("upgrade", "head")).returncode == 0
        async with database() as db:
            claim = await db.get(ImportCapacity, entry_id)
            assert claim is not None and not claim.observed_mounts
            with pytest.raises(capacity.CapacityWait, match="Existing import storage"):
                await capacity.require_observed_imports(db)
        await execution.execute(UUID(run["entries"][0]["operation_id"]))
        assert (await state(client, run))["state"] == "confirmed"
        async with database() as db:
            await capacity.require_observed_imports(db)
            assert not (await db.get(ImportCapacity, entry_id)).resources
    finally:
        assert (await migrate("upgrade", "head")).returncode == 0
        await get_engine().dispose()
