# ruff: noqa: F811
"""Reviewed credential changes can observe an existing transfer, never add it again."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.adapters.contracts import AdapterError, FailureKind
from app.config import get_settings
from app.db.models import (
    AcquisitionSelection,
    DownloadAttempt,
    DownloadIdentityClaim,
    DownloadRepair,
    ImportDestination,
    Integration,
    Library,
    Operation,
    SourceConnection,
    User,
)
from app.domain import download_attempts as downloads
from app.importing.destinations import destination_configuration
from app.importing.naming import fingerprint
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_download_attempts import downloader, selected, start  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def submitted(client, database, selected, downloader):
    response = await start(client, selected)
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    await downloads.run(identifier)
    return identifier


async def change(database, selected, kind="downloader"):
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        if kind == "source":
            (await db.get(SourceConnection, "mam")).generation += 1
        elif kind == "backend":
            destination = await db.get(ImportDestination, selection.destination_id)
            library = await db.get(Library, destination.library_id)
            backend = await db.get(Integration, library.integration_id)
            backend.credential_generation += 1
            await db.flush()
            configuration = await destination_configuration(db, destination)
            destination.probe = {
                **destination.probe,
                "configuration_revision": fingerprint(configuration),
            }  # Contract fixture; actual probes have their own filesystem/ABS suite.
        elif kind == "copy":
            # Hardlinks fail on some filesystems, so the library switches to copying files.
            destination = await db.get(ImportDestination, selection.destination_id)
            assert destination.mode == "hardlink"
            destination.mode = "copy"
            await db.flush()
            configuration = await destination_configuration(db, destination)
            destination.probe = {
                **destination.probe,
                "copy": True,
                "configuration_revision": fingerprint(configuration),
            }
        else:
            (await db.get(Integration, selection.downloader_id)).credential_generation += 1


async def preview(client, identifier):
    response = await client.get(f"/api/acquisition/downloads/{identifier}/repair-preview")
    assert response.status_code == 200, response.text
    return response.json()


async def repair(client, identifier, proposal, key="repair-existing-transfer"):
    return await client.post(
        f"/api/acquisition/downloads/{identifier}/repairs",
        json={"revision": proposal["revision"]},
        headers={"Idempotency-Key": key},
    )


async def state(database, identifier):
    async with database() as db:
        attempt = await db.get(DownloadAttempt, identifier)
        row = await db.scalar(select(DownloadRepair).where(DownloadRepair.attempt_id == identifier))
        return attempt, row


@pytest.mark.parametrize("kind", ["downloader", "source", "backend", "copy"])
async def test_reviewed_generations_resume_same_transfer_and_preserve_frozen_selection(
    client, database, selected, submitted, downloader, kind
):
    async with database() as db:
        frozen = (await db.get(AcquisitionSelection, UUID(selected["id"]))).frozen
    assert not (await client.get(f"/api/acquisition/downloads/{submitted}")).json()["can_repair"]
    await change(database, selected, kind)
    await downloads.run(submitted)
    assert (await state(database, submitted))[0].state == "held"
    assert (await client.get(f"/api/acquisition/downloads/{submitted}")).json()["can_repair"]
    proposal = await preview(client, submitted)
    assert len(proposal["changes"]) == 1
    response = await repair(client, submitted, proposal)
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "pending"
    assert (await repair(client, submitted, proposal)).json()["id"] == response.json()["id"]
    assert (await client.post(f"/api/acquisition/downloads/{submitted}/recheck")).status_code == 409
    await downloads.run(submitted)
    attempt, row = await state(database, submitted)
    assert attempt.state == "downloading" and row.state == "applied"
    assert row.applied_at
    await downloads.run(submitted)
    assert (await state(database, submitted))[0].state == "downloading"
    assert downloader.calls.count("submit") == 1
    async with database() as db:
        assert (await db.get(AcquisitionSelection, UUID(selected["id"]))).frozen == frozen
        assert all(item.active for item in await db.scalars(select(DownloadIdentityClaim)))
        assert (await db.get(Operation, row.operation_id)).status == "completed"
    detail = (await client.get(f"/api/acquisition/downloads/{submitted}")).json()
    assert detail["repair"]["state"] == "applied"
    assert not detail["can_repair"]
    assert "configuration" not in detail["repair"]
    assert "private-password" not in str(detail) and "mam_id" not in str(detail)


async def test_concurrent_and_stale_repair_commands_are_fenced(
    client, database, selected, submitted
):
    await change(database, selected)
    old = await preview(client, submitted)
    await change(database, selected)
    assert (await repair(client, submitted, old)).status_code == 409
    fresh = await preview(client, submitted)
    results = await asyncio.gather(*(repair(client, submitted, fresh) for _ in range(3)))
    assert all(row.status_code == 202 for row in results)
    assert len({row.json()["id"] for row in results}) == 1
    assert (await repair(client, submitted, fresh, "different-repair-command")).status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadRepair)) == 1


@pytest.mark.parametrize("field", ["endpoint", "category", "path", "library", "probe"])
async def test_repair_rejects_route_or_destination_changes(
    client, database, selected, submitted, field
):
    await change(database, selected)
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        downloader = await db.get(Integration, selection.downloader_id)
        destination = await db.get(ImportDestination, selection.destination_id)
        if field == "endpoint":
            downloader.base_url = "http://another-downloader.test"
        elif field == "category":
            downloader.config = {**downloader.config, "category": "different"}
        elif field == "path":
            downloader.config = {**downloader.config, "save_path": "/downloads/another"}
        elif field == "library":
            destination.backend_path = "/another-library-root"
        else:
            destination.probe = None
    response = await client.get(f"/api/acquisition/downloads/{submitted}/repair-preview")
    assert response.status_code == 409, response.text
    async with database() as db:
        assert not await db.scalar(select(DownloadRepair.id))


@pytest.mark.parametrize("actor_change", [False, True])
async def test_configuration_or_actor_change_during_network_check_does_not_apply_repair(
    client, database, admin, selected, submitted, downloader, actor_change
):
    await change(database, selected)
    response = await repair(client, submitted, await preview(client, submitted))
    assert response.status_code == 202

    async def interrupt():
        if actor_change:
            async with database() as db, db.begin():
                (await db.get(User, UUID(admin["id"]))).role = "member"
        else:
            await change(database, selected)

    downloader.before_find = interrupt
    await downloads.run(submitted)
    attempt, row = await state(database, submitted)
    assert attempt.state == row.state == "held"
    assert row.applied_at is None and downloader.calls.count("submit") == 1


async def test_pending_repair_survives_invisible_transfer_and_transient_failure(
    client, database, selected, submitted, downloader
):
    await change(database, selected)
    assert (await repair(client, submitted, await preview(client, submitted))).status_code == 202
    observed, downloader.states = downloader.states, []
    await downloads.run(submitted)
    attempt, row = await state(database, submitted)
    assert attempt.state == "uncertain" and row.state == "pending"

    async def unavailable():
        raise AdapterError(FailureKind.UNAVAILABLE, "Fixture service unavailable")

    downloader.before_find = unavailable
    await downloads.run(submitted)
    assert (await state(database, submitted))[1].state == "pending"
    downloader.states = observed
    await downloads.run(submitted)
    assert (await state(database, submitted))[1].state == "applied"
    assert downloader.calls.count("submit") == 1


async def test_conflicting_transfer_cannot_validate_reviewed_connections(
    client, database, selected, submitted, downloader
):
    await change(database, selected)
    await repair(client, submitted, await preview(client, submitted))
    downloader.states = [downloader.states[0].model_copy(update={"tags": {"unrelated"}})]
    await downloads.run(submitted)
    attempt, row = await state(database, submitted)
    assert row.state == "held" and row.applied_at is None
    assert attempt.external_may_exist and downloader.calls.count("submit") == 1


async def test_process_death_leaves_pending_repair_and_lease_for_read_only_recovery(
    client, database, selected, submitted, downloader
):
    await change(database, selected)
    await repair(client, submitted, await preview(client, submitted))

    async def killed():
        raise asyncio.CancelledError()

    downloader.before_find = killed
    with pytest.raises(asyncio.CancelledError):
        await downloads.run(submitted)
    attempt, row = await state(database, submitted)
    assert row.state == "pending" and attempt.lease_until > datetime.now(UTC)
    calls = len(downloader.calls)
    await downloads.run(submitted)
    assert len(downloader.calls) == calls
    async with database() as db, db.begin():
        (await db.get(DownloadAttempt, submitted)).lease_until = datetime.now(UTC) - timedelta(
            seconds=1
        )
    await downloads.run(submitted)
    assert (await state(database, submitted))[1].state == "applied"
    assert downloader.calls.count("submit") == 1


async def test_repair_permission_idempotency_and_recovery_boundaries(
    client, database, admin, selected, submitted, monkeypatch
):
    assert (
        await client.get(f"/api/acquisition/downloads/{submitted}/repair-preview")
    ).status_code == 409
    await change(database, selected)
    proposal = await preview(client, submitted)
    monkeypatch.setattr(get_settings(), "recovery_mode", True)
    assert (await repair(client, submitted, proposal)).status_code == 409
    monkeypatch.setattr(get_settings(), "recovery_mode", False)
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", False)
    assert (await repair(client, submitted, proposal)).status_code == 202
    assert (await repair(client, submitted, {"revision": "0" * 64})).status_code == 409
    assert (
        await client.get(f"/api/acquisition/downloads/{uuid4()}/repair-preview")
    ).status_code == 404
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "member"
    assert (await repair(client, submitted, proposal)).status_code == 403


async def test_repair_history_refuses_lossy_rollback(client, database, selected, submitted):
    from tests.integration.test_correction_migration import legacy_request_policy_fixture, migrate

    await change(database, selected)
    await repair(client, submitted, await preview(client, submitted))
    async with database() as db:
        before = await db.scalar(text("SELECT version_num FROM alembic_version"))
    await legacy_request_policy_fixture(database)
    result = await migrate("downgrade", "0019_fulfillment")
    assert result.returncode != 0 and "Capacity history requires" in result.stderr
    async with database() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == before


async def test_failed_enqueue_rolls_back_review_and_attempt_changes(
    client, database, selected, submitted, monkeypatch
):
    from app.domain import download_repairs

    await change(database, selected)
    proposal = await preview(client, submitted)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("Fixture queue failure")

    monkeypatch.setattr(download_repairs, "enqueue", unavailable)
    with pytest.raises(RuntimeError, match="Fixture queue failure"):
        await repair(client, submitted, proposal)
    async with database() as db:
        assert not await db.scalar(select(DownloadRepair.id))
        assert not await db.scalar(
            select(Operation.id).where(Operation.kind == "acquisition.repair")
        )
        assert (await db.get(DownloadAttempt, submitted)).state == "downloading"


async def test_later_review_preserves_prior_repair_and_can_finish_inspection_handoff(
    client, database, selected, submitted, downloader
):
    for count in range(2):
        await change(database, selected)
        response = await repair(
            client, submitted, await preview(client, submitted), f"repair-generation-{count}"
        )
        assert response.status_code == 202, response.text
        if count:
            downloader.states = [
                row.model_copy(
                    update={
                        "completed": True,
                        "reported_complete": True,
                        "state": "uploading",
                        "progress": 1,
                        "files": [file.model_copy(update={"complete": True}) for file in row.files],
                    }
                )
                for row in downloader.states
            ]
        await downloads.run(submitted)
    async with database() as db:
        rows = list(await db.scalars(select(DownloadRepair).order_by(DownloadRepair.created_at)))
        assert [row.state for row in rows] == ["applied", "applied"]
        assert [row.configuration["downloader"]["generation"] for row in rows] == [2, 3]
        attempt = await db.get(DownloadAttempt, submitted)
        assert attempt.state == "complete" and attempt.inspection_id
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        assert selection.frozen["downloader"]["generation"] == 1
    assert downloader.calls.count("submit") == 1


async def test_completed_repaired_transfer_with_already_satisfied_request_does_not_reimport(
    client, database, catalog, selected, submitted, downloader
):
    from tests.integration.test_download_fulfillment import asset

    await change(database, selected)
    await repair(client, submitted, await preview(client, submitted))
    await asset(database, catalog)
    downloader.states = [
        row.model_copy(
            update={
                "completed": True,
                "reported_complete": True,
                "state": "uploading",
                "progress": 1,
                "files": [file.model_copy(update={"complete": True}) for file in row.files],
            }
        )
        for row in downloader.states
    ]
    await downloads.run(submitted)
    attempt, row = await state(database, submitted)
    assert attempt.state == "complete" and row.state == "applied"
    assert attempt.inspection_id is None
    async with database() as db:
        assert (await db.get(AcquisitionSelection, UUID(selected["id"]))).state == "fulfilled"
    assert downloader.calls.count("submit") == 1
