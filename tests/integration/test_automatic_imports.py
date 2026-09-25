# ruff: noqa: F811
import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db.models import (
    AutomaticImport,
    AutomaticImportPolicy,
    DownloadAttempt,
    DownloadHandoff,
    DownloadInspection,
    ImportDestination,
    Operation,
    User,
)
from app.domain import download_attempts as downloads
from app.importing import automatic
from app.jobs.tasks import schedule_downloads
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_correction_migration import legacy_request_policy_fixture, migrate
from tests.integration.test_download_attempts import downloader, selected, start  # noqa: F401

pytestmark = pytest.mark.integration


async def policy(client, selected, *, enabled=True, generation=0):
    return await client.put(
        f"/api/organization/destinations/{selected['destination_id']}/automatic-import",
        json={
            "enabled": enabled,
            "expected_generation": generation,
            "destination_revision": selected["destination_revision"],
        },
    )


@pytest.fixture
async def automatic_job(client, admin, database, selected, selection_route, downloader):
    approved = await policy(client, selection_route)
    assert approved.status_code == 200, approved.text
    downloader.complete = True
    response = await start(client, selected)
    assert response.status_code == 202, response.text
    await downloads.run(UUID(response.json()["id"]))
    async with database() as db:
        row = await db.scalar(select(AutomaticImport))
        assert row and row.state == "queued"
        assert not (await db.get(DownloadAttempt, row.attempt_id)).inspection_id
        return row.id


async def test_policy_defaults_privacy_stale_edit_and_no_backlog(
    client, admin, database, selected, selection_route
):
    endpoint = (
        f"/api/organization/destinations/{selection_route['destination_id']}/automatic-import"
    )
    initial = (await client.get(endpoint)).json()
    assert not initial["enabled"] and initial["generation"] == 0
    assert initial["requested_enabled"]
    assert (await policy(client, selection_route)).json()["ready"]
    assert (await policy(client, selection_route)).status_code == 409
    disabled = await policy(client, selection_route, enabled=False, generation=1)
    assert disabled.status_code == 200 and not disabled.json()["enabled"]
    async with database() as db, db.begin():
        assert not await db.scalar(select(AutomaticImport.id))
        assert not await db.scalar(select(DownloadAttempt.id))
        (await db.get(User, UUID(admin["id"]))).role = "member"
    assert (await client.get(endpoint)).status_code == 403
    assert (await policy(client, selection_route, generation=2)).status_code == 403


async def test_pending_preference_is_saved_without_approving_imports(
    client, admin, database, selected, selection_route
):
    endpoint = (
        f"/api/organization/destinations/{selection_route['destination_id']}/automatic-import"
    )
    # Saving a preference must not approve a route, even if the route is already verified.
    response = await client.put(
        endpoint,
        json={
            "enabled": True,
            "defer_until_verified": True,
            "expected_generation": 0,
            "destination_revision": selection_route["destination_revision"],
        },
    )
    assert response.status_code == 200, response.text
    pending = response.json()
    assert pending["requested_enabled"] and not pending["enabled"] and not pending["ready"]
    async with database() as db:
        stored = await db.scalar(select(AutomaticImportPolicy))
        assert not stored.enabled
        assert stored.configuration == {"requested_enabled": True}
        assert not await db.scalar(select(AutomaticImport.id))
    disabled = await policy(client, selection_route, enabled=False, generation=1)
    assert disabled.status_code == 200, disabled.text
    assert not disabled.json()["requested_enabled"]
    assert not (await client.get(endpoint)).json()["requested_enabled"]
    enabled = await policy(client, selection_route, generation=2)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["ready"] and enabled.json()["requested_enabled"]


@pytest.mark.parametrize("change", ["probe", "route", "recovery"])
async def test_policy_requires_current_certified_route(
    client, database, selected, selection_route, monkeypatch, change
):
    if change == "recovery":
        monkeypatch.setattr(get_settings(), "recovery_mode", True)
    else:
        async with database() as db, db.begin():
            row = await db.get(ImportDestination, UUID(selection_route["destination_id"]))
            if change == "probe":
                row.probe = None
            else:
                row.backend_path = "/changed-root"
    result = await policy(client, selection_route)
    assert result.status_code == 409, result.text
    async with database() as db:
        assert not await db.scalar(select(AutomaticImportPolicy.id))


async def test_duplicate_workers_create_one_inspection_and_never_submit_again(
    database, automatic_job, downloader
):
    await asyncio.gather(*(automatic.run(automatic_job) for _ in range(3)))
    async with database() as db:
        row = await db.get(AutomaticImport, automatic_job)
        assert row.state == "inspecting" and row.inspection_id
        assert await db.scalar(select(func.count()).select_from(DownloadInspection)) == 1
        assert await db.scalar(select(func.count()).select_from(DownloadHandoff)) == 1
    assert downloader.calls.count("submit") == 1


@pytest.mark.parametrize("change", ["disabled", "reapproved", "approver", "route", "recovery"])
async def test_queued_automatic_work_rechecks_approval(
    client, admin, database, automatic_job, selection_route, monkeypatch, change
):
    if change == "recovery":
        monkeypatch.setattr(get_settings(), "recovery_mode", True)
    elif change in {"disabled", "reapproved"}:
        response = await policy(
            client, selection_route, enabled=change == "reapproved", generation=1
        )
        assert response.status_code == 200
    else:
        async with database() as db, db.begin():
            if change == "approver":
                (await db.get(User, UUID(admin["id"]))).active = False
            else:
                (
                    await db.get(ImportDestination, UUID(selection_route["destination_id"]))
                ).probe = None
    await automatic.run(automatic_job)
    async with database() as db:
        row = await db.get(AutomaticImport, automatic_job)
        assert row.state == "held" and row.inspection_id is None
        assert not await db.scalar(select(DownloadHandoff.id))
        assert (await db.get(Operation, row.operation_id)).status == "failed"


async def test_inspection_enqueue_failure_rolls_back_handoff_and_can_resume(
    database, automatic_job, monkeypatch
):
    original = downloads.enqueue

    async def fail(*args, **kwargs):
        raise RuntimeError("queue temporarily unavailable")

    monkeypatch.setattr(downloads, "enqueue", fail)
    with pytest.raises(RuntimeError, match="queue temporarily unavailable"):
        await automatic.run(automatic_job)
    async with database() as db:
        row = await db.get(AutomaticImport, automatic_job)
        assert row.state == "queued" and row.inspection_id is None
        assert not await db.scalar(select(DownloadHandoff.id))
    monkeypatch.setattr(downloads, "enqueue", original)
    await automatic.run(automatic_job)
    async with database() as db:
        assert (await db.get(AutomaticImport, automatic_job)).state == "inspecting"


async def test_periodic_recovery_repairs_lost_continuation_without_duplicate_jobs(
    database, automatic_job
):
    await automatic.run(automatic_job)
    async with database() as db, db.begin():
        row = await db.get(AutomaticImport, automatic_job)
        inspection = await db.get(DownloadInspection, row.inspection_id)
        inspection.state, inspection.message = "failed", "Fixture lost inspection callback"
        operation = await db.get(Operation, row.operation_id)
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status='succeeded' WHERE id=:id"),
            {"id": operation.job_id},
        )
        before = operation.job_id
    await schedule_downloads(100)
    async with database() as db:
        after = (await db.get(Operation, row.operation_id)).job_id
        assert after != before
    await schedule_downloads(101)
    async with database() as db:
        assert (await db.get(Operation, row.operation_id)).job_id == after
    await automatic.run(automatic_job)
    async with database() as db:
        assert (await db.get(AutomaticImport, automatic_job)).state == "held"


async def test_automatic_history_cannot_be_discarded_by_downgrade(database, automatic_job):
    async with database() as db:
        before = await db.scalar(text("SELECT version_num FROM alembic_version"))
    await legacy_request_policy_fixture(database)
    result = await migrate("downgrade", "0021_handoffs")
    assert result.returncode != 0 and "Capacity history requires" in result.stderr
    async with database() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == before


@pytest.mark.parametrize("stage", ["automatic", "inspection"])
@pytest.mark.parametrize("status", ["failed", "aborted"])
async def test_recovery_holds_exhausted_jobs_without_restarting_retry_budget(
    database, automatic_job, stage, status
):
    if stage == "inspection":
        await automatic.run(automatic_job)
    async with database() as db, db.begin():
        row = await db.get(AutomaticImport, automatic_job)
        operation = await db.get(Operation, row.operation_id)
        original_job = operation.job_id
        terminal_job = original_job
        if stage == "inspection":
            inspection = await db.get(DownloadInspection, row.inspection_id)
            terminal_job = (await db.get(Operation, inspection.operation_id)).job_id
            await db.execute(
                text("UPDATE book_queue.procrastinate_jobs SET status='succeeded' WHERE id=:id"),
                {"id": original_job},
            )
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status=:status WHERE id=:id"),
            {"id": terminal_job, "status": status},
        )
    await asyncio.gather(schedule_downloads(200), schedule_downloads(201))
    await schedule_downloads(202)
    async with database() as db:
        row = await db.get(AutomaticImport, automatic_job)
        operation = await db.get(Operation, row.operation_id)
        assert row.state == "held" and "retries stopped" in row.message
        assert operation.status == "failed" and operation.job_id == original_job
        if stage == "inspection":
            assert (await db.get(DownloadInspection, row.inspection_id)).state == "failed"


async def test_recheck_resumes_a_held_import_and_request_counts_follow_review(
    client, database, automatic_job, downloader
):
    async with database() as db, db.begin():
        row = await db.get(AutomaticImport, automatic_job)
        attempt_id = row.attempt_id
        row.state, row.message = "held", "No matching edition identifier"
        operation = await db.get(Operation, row.operation_id)
        operation.status = "failed"
    counts = (await client.get("/api/requests/counts")).json()
    assert counts["review"] == 1 and counts["downloading"] == 0
    review = (await client.get("/api/requests?status=review")).json()
    assert review["items"][0]["targets"][0]["needs_review"]
    response = await client.post(f"/api/acquisition/downloads/{attempt_id}/recheck")
    assert response.status_code == 202, response.text
    counts = (await client.get("/api/requests/counts")).json()
    assert counts["review"] == 0 and counts["downloading"] == 1
    await automatic.run(automatic_job)
    async with database() as db:
        assert (await db.get(AutomaticImport, automatic_job)).state == "inspecting"
    assert downloader.calls.count("submit") == 1


async def test_linked_review_retries_automatic_matching_without_new_inspection(
    client, database, automatic_job, downloader
):
    await automatic.run(automatic_job)
    async with database() as db, db.begin():
        row = await db.get(AutomaticImport, automatic_job)
        inspection_id = row.inspection_id
        inspection = await db.get(DownloadInspection, inspection_id)
        inspection.state = "ready"
        row.state, row.message = "held", "No matching edition"
    endpoint = f"/api/organization/inspections/{inspection_id}"
    view = (await client.get(endpoint)).json()
    assert view["download"]["can_retry"]
    assert view["plan_id"] is None
    response = await client.post(endpoint + "/retry")
    assert response.status_code == 202, response.text
    assert response.json()["download"]["state"] == "inspecting"
    assert response.json()["id"] == str(inspection_id)
    assert not response.json()["download"]["can_retry"]
    assert (await client.post(endpoint + "/retry")).status_code == 409
    assert downloader.calls.count("submit") == 1
