# ruff: noqa: F401, F811
import asyncio
import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, func, select, text

from app.db.models import (
    AutomaticImportContinuation,
    AutomaticImportPolicy,
    DownloadAttempt,
    DownloadCapacity,
    DownloadIdentityClaim,
    DownloadMembership,
    Operation,
)
from app.domain import automatic_packs as packs
from app.domain import automatic_selection as automatic
from app.domain import download_attempts as downloads
from app.importing import reuse
from tests.integration.test_acquisition import catalog
from tests.integration.test_acquisition_selections import selection_route
from tests.integration.test_automatic_dispatch import authorized
from tests.integration.test_automatic_pack_selection import series_pack
from tests.integration.test_automatic_packs import pair
from tests.integration.test_automatic_selection import detail, source

pytestmark = pytest.mark.integration


async def first_transfer(client, database, pair):
    await automatic.run(pair[0])
    async with database() as db, db.begin():
        op = await db.get(Operation, pair[0])
        payload = deepcopy(op.payload)
        payload["pack_dispatch"]["deadline"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        op.payload = payload
    await packs.run(pair[0])
    result = await detail(client, pair[0])
    assert result["download_id"], result
    return UUID(result["download_id"])


@pytest.mark.parametrize("state", ["queued", "downloading", "complete"])
async def test_later_request_joins_saved_transfer_without_rewriting_receipt(
    client, database, pair, authorized, state
):
    identifier = await first_transfer(client, database, pair)
    if state != "queued":
        authorized["qbit"].complete = state == "complete"
        await downloads.run(identifier)
    async with database() as db:
        attempt = await db.get(DownloadAttempt, identifier)
        assert attempt.state == state
        original = deepcopy((await db.get(Operation, attempt.operation_id)).payload)
    await automatic.run(pair[1])
    await asyncio.gather(packs.run(pair[1]), packs.run(pair[1]))
    second = await detail(client, pair[1])
    assert second["status"] == "completed" and second["download_id"] == str(identifier), second
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
        assert await db.scalar(select(func.count()).select_from(DownloadCapacity)) == 1
        assert await db.scalar(select(func.count()).select_from(DownloadIdentityClaim)) == 1
        memberships = list(await db.scalars(select(DownloadMembership)))
        assert len(memberships) == 2
        joined = next(item for item in memberships if item.join_operation_id)
        receipt = await db.get(Operation, joined.join_operation_id)
        assert receipt.payload["selection_ids"] == [second["selection_id"]]
        assert receipt.payload["attempt_id"] == str(identifier)
        assert (await db.get(Operation, attempt.operation_id)).payload == original
        continuations = list(await db.scalars(select(AutomaticImportContinuation)))
        assert len(continuations) == (1 if state == "complete" else 0)
        if continuations:
            assert continuations[0].evidence["authorized_selection_ids"] == [second["selection_id"]]
    await downloads.run(identifier)
    assert authorized["qbit"].calls.count("submit") == 1


async def test_uncertain_existing_transfer_holds_join_without_new_attempt(
    client, database, pair, authorized
):
    identifier = await first_transfer(client, database, pair)
    async with database() as db, db.begin():
        attempt = await db.get(DownloadAttempt, identifier)
        attempt.state, attempt.external_may_exist = "held", True
    await automatic.run(pair[1])
    await packs.run(pair[1])
    second = await detail(client, pair[1])
    assert second["status"] == "held" and not second["download_id"], second
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadMembership)) == 1
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
    assert "submit" not in authorized["qbit"].calls


async def test_join_during_live_preflight_is_included_before_submission(
    client, database, pair, authorized
):
    identifier = await first_transfer(client, database, pair)

    async def join_while_observing():
        async with database() as db:
            assert (await db.get(DownloadAttempt, identifier)).state == "preflight"
        await automatic.run(pair[1])
        await packs.run(pair[1])
        second = await detail(client, pair[1])
        assert second["download_id"] == str(identifier), second

    authorized["qbit"].before_find = join_while_observing
    await downloads.run(identifier)
    async with database() as db:
        assert (await db.get(DownloadAttempt, identifier)).state == "downloading"
        assert await db.scalar(select(func.count()).select_from(DownloadMembership)) == 2
        assert await db.scalar(select(func.count()).select_from(DownloadCapacity)) == 1
    assert authorized["qbit"].calls.count("submit") == 1


async def completed_join(client, database, pair, authorized):
    identifier = await first_transfer(client, database, pair)
    authorized["qbit"].complete = True
    await downloads.run(identifier)
    await automatic.run(pair[1])
    await packs.run(pair[1])
    async with database() as db:
        return await db.scalar(select(AutomaticImportContinuation))


@pytest.mark.parametrize("stopped", ["missing", "succeeded", "failed", "aborted"])
async def test_reuse_scheduler_reaches_stopped_work_behind_busy_jobs(
    client, database, pair, authorized, tmp_path, stopped
):
    from app.jobs.queue import enqueue
    from app.jobs.tasks import schedule_downloads

    original = await completed_join(client, database, pair, authorized)
    now = datetime.now(UTC)
    payload = {"saved_evidence": "x" * 100_000}
    busy = []
    async with database() as db, db.begin():
        (await db.get(AutomaticImportContinuation, original.id)).state = "held"
        owner = (await db.get(Operation, original.operation_id)).owner_id
        for n in range(21):
            join = Operation(owner_id=owner, kind="acquisition.reuse", idempotency_key=f"join-{n}")
            operation = Operation(
                owner_id=owner,
                kind=reuse.KIND,
                idempotency_key=f"recover-reuse-{n}",
                payload=payload,
            )
            db.add_all([join, operation])
            await db.flush()
            row = AutomaticImportContinuation(
                attempt_id=original.attempt_id,
                join_operation_id=join.id,
                policy_id=original.policy_id,
                policy_generation=original.policy_generation,
                operation_id=operation.id,
                evidence={**original.evidence, **payload},
                created_at=now - timedelta(hours=1) + timedelta(seconds=n),
                state="inspecting" if n % 2 else "queued",
            )
            db.add(row)
            await db.flush()
            if n < 20 or stopped != "missing":
                operation.job_id = await enqueue(db, reuse.KIND, continuation_id=str(row.id))
                await db.execute(
                    text("UPDATE book_queue.procrastinate_jobs SET status=:state WHERE id=:id"),
                    {
                        "id": operation.job_id,
                        "state": ("doing" if n % 2 else "todo") if n < 20 else stopped,
                    },
                )
            if n < 20:
                busy.append((row.id, operation.id, operation.job_id))
            else:
                eligible_id, operation_id, prior_job = row.id, operation.id, operation.job_id
    calls, loaded = [], []
    engine = database.kw["bind"].sync_engine

    def count(*args):
        calls.append(1)

    def capture(row, context):
        loaded.append({k: v for k, v in vars(row).items() if not k.startswith("_")})

    event.listen(engine, "before_cursor_execute", count)
    for model in (AutomaticImportContinuation, Operation):
        event.listen(model, "load", capture)
    started = time.perf_counter()
    try:
        await schedule_downloads(0)
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", count)
        for model in (AutomaticImportContinuation, Operation):
            event.remove(model, "load", capture)
    metrics = {
        "queries": len(calls),
        "loaded_bytes": len(json.dumps(loaded, default=str)),
        "elapsed_ms": elapsed * 1000,
    }
    (tmp_path / "reuse-scheduler-metrics.json").write_text(json.dumps(metrics, indent=2))
    async with database() as db:
        eligible = await db.get(AutomaticImportContinuation, eligible_id)
        operation = await db.get(Operation, operation_id)
        if stopped in {"failed", "aborted"}:
            assert eligible.state == "held" and operation.status == "failed"
            assert operation.job_id == prior_job
        else:
            assert operation.job_id is not None and operation.job_id != prior_job
        new_job = operation.job_id
        assert eligible.evidence["saved_evidence"] == payload["saved_evidence"]
        for row_id, op_id, job_id in busy:
            assert (await db.get(AutomaticImportContinuation, row_id)).state in {
                "queued",
                "inspecting",
            }
            assert (await db.get(Operation, op_id)).job_id == job_id
    exhausted = stopped in {"failed", "aborted"}
    async with database() as db, db.begin():
        before_jobs = await db.scalar(
            text(
                "SELECT count(*) FROM book_queue.procrastinate_jobs "
                "WHERE args->>'continuation_id'=:id"
            ),
            {"id": str(eligible_id)},
        )
        if not exhausted:
            await db.execute(
                text("UPDATE book_queue.procrastinate_jobs SET status='succeeded' WHERE id=:id"),
                {"id": new_job},
            )
    await asyncio.gather(schedule_downloads(1), schedule_downloads(2))
    async with database() as db:
        final_job = (await db.get(Operation, operation_id)).job_id
        assert (final_job == new_job) if exhausted else (final_job != new_job)
        assert await db.scalar(
            text(
                "SELECT count(*) FROM book_queue.procrastinate_jobs "
                "WHERE args->>'continuation_id'=:id"
            ),
            {"id": str(eligible_id)},
        ) == before_jobs + (0 if exhausted else 1)
    assert metrics["queries"] < 40
    assert metrics["loaded_bytes"] < 10_000


@pytest.mark.parametrize("job_state", ["failed", "aborted"])
async def test_stopped_reuse_is_visible_and_recheck_is_deduplicated(
    client, database, pair, authorized, job_state
):
    row = await completed_join(client, database, pair, authorized)
    async with database() as db, db.begin():
        operation = await db.get(Operation, row.operation_id)
        old_job = operation.job_id
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status=:status WHERE id=:id"),
            {"status": job_state, "id": old_job},
        )
        await reuse.recover(db, row.id)
    view = (await client.get(f"/api/acquisition/downloads/{row.attempt_id}")).json()
    assert view["import_continuations"][0]["state"] == "held", view
    assert "stopped" in view["import_continuations"][0]["message"]
    assert sum(bool(item["join_operation_id"]) for item in view["members"]) == 1
    async with database() as db, db.begin():
        attempt = await db.get(DownloadAttempt, row.attempt_id)
        await reuse.recheck(db, attempt)
        operation = await db.get(Operation, row.operation_id)
        new_job = operation.job_id
        assert new_job != old_job
        await reuse.recheck(db, attempt)
        assert operation.job_id == new_job
        current = await db.get(AutomaticImportContinuation, row.id)
        assert current.created_at == row.created_at and current.state == "queued"
    assert authorized["qbit"].calls.count("submit") == 1


async def test_reuse_verification_expiry_does_not_reset_itself(client, database, pair, authorized):
    row = await completed_join(client, database, pair, authorized)
    async with database() as db, db.begin():
        current = await db.get(AutomaticImportContinuation, row.id)
        current.evidence = {
            **current.evidence,
            "verification_started_at": (datetime.now(UTC) - timedelta(minutes=16)).isoformat(),
        }
    await reuse.run(row.id)
    async with database() as db:
        current = await db.get(AutomaticImportContinuation, row.id)
        assert current.state == "held" and "expired" in current.message
        assert current.created_at == row.created_at
    assert authorized["qbit"].calls.count("submit") == 1


async def test_reapproval_cannot_silently_authorize_a_joined_import(
    client, database, pair, authorized
):
    from fastapi import HTTPException

    row = await completed_join(client, database, pair, authorized)
    async with database() as db, db.begin():
        (await db.get(AutomaticImportPolicy, row.policy_id)).generation += 1
    await reuse.run(row.id)
    async with database() as db, db.begin():
        current = await db.get(AutomaticImportContinuation, row.id)
        assert current.state == "held" and "approval changed" in current.message
        with pytest.raises(HTTPException, match="approval changed"):
            await reuse.recheck(db, await db.get(DownloadAttempt, row.attempt_id))
    assert authorized["qbit"].calls.count("submit") == 1
