# ruff: noqa: F811
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    AcquisitionProfile,
    AcquisitionReservation,
    AcquisitionSelection,
    AutomaticImport,
    AutomaticImportPolicy,
    DownloadAttempt,
    DownloadCapacity,
    DownloadIdentityClaim,
    ImportDestination,
    Operation,
    User,
)
from app.domain import automatic_selection as automatic
from app.domain import download_attempts as downloads
from app.domain.release_profiles import ProfileSnapshot, ReleasePreferences
from app.jobs.retry import SourceSearchRetry
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_automatic_selection import detail, source, start  # noqa: F401
from tests.integration.test_download_attempts import Client

pytestmark = pytest.mark.integration


@pytest.fixture
async def authorized(client, database, source, monkeypatch):
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    body = source["body"]
    approved = await client.put(
        f"/api/organization/destinations/{body['destination_id']}/automatic-import",
        json={
            "enabled": True,
            "expected_generation": 0,
            "destination_revision": body["destination_revision"],
        },
    )
    assert approved.status_code == 200 and approved.json()["ready"], approved.text
    body["download_when_ready"] = True
    qbit = Client(database, source["descriptor"].model_dump(mode="json"))
    monkeypatch.setattr(downloads, "QbitClient", lambda *args: qbit)
    return {**source, "qbit": qbit}


async def policy_change(database, kind="disabled"):
    async with database() as db, db.begin():
        policy = await db.scalar(select(AutomaticImportPolicy))
        if kind == "disabled":
            policy.enabled = False
        else:
            policy.generation += 1


@pytest.mark.parametrize("missing", ["dispatch", "approval", "recovery"])
async def test_automatic_consent_requires_installation_and_standing_import_permission(
    client, database, source, monkeypatch, missing
):
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", missing != "dispatch")
    monkeypatch.setattr(get_settings(), "recovery_mode", missing == "recovery")
    source["body"]["download_when_ready"] = True
    result = await client.post(
        "/api/acquisition/automatic-selections",
        json=source["body"],
        headers={"Idempotency-Key": "unauthorized-auto-dispatch"},
    )
    assert result.status_code == 409, result.text
    if missing == "dispatch":
        assert "BOOK_DOWNLOAD_DISPATCH_ENABLED=true" in result.json()["detail"]
    elif missing == "recovery":
        assert "recovery" in result.json()["detail"]
        assert "BOOK_DOWNLOAD_DISPATCH_ENABLED" not in result.json()["detail"]
    async with database() as db:
        assert not await db.scalar(select(DownloadAttempt.id))
        assert not await db.scalar(select(Operation.id).where(Operation.kind == automatic.KIND))


async def test_selection_and_automatic_attempt_are_atomic_idempotent_and_submit_once(
    client, database, authorized
):
    saved = await start(client, authorized)
    responses = await asyncio.gather(
        *(automatic.run(UUID(saved["id"])) for _ in range(2)), return_exceptions=True
    )
    assert all(value is None or isinstance(value, SourceSearchRetry) for value in responses), (
        responses
    )
    view = await detail(client, saved["id"])
    assert view["status"] == "completed" and view["download_when_ready"] and view["download_id"]
    assert (await start(client, authorized))["download_id"] == view["download_id"]
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
        assert await db.scalar(select(func.count()).select_from(AcquisitionSelection)) == 1
        assert await db.scalar(select(func.count()).select_from(DownloadIdentityClaim)) == 1
        attempt = await db.get(DownloadAttempt, UUID(view["download_id"]))
        assert (await db.get(DownloadCapacity, attempt.id)).automatic
        assert (await db.get(Operation, attempt.operation_id)).payload["automatic"]
    await downloads.run(attempt.id)
    await automatic.run(UUID(saved["id"]))
    await downloads.run(attempt.id)
    assert authorized["qbit"].calls.count("submit") == 1


@pytest.mark.parametrize("change", ["disabled", "reapproved", "approver", "destination"])
async def test_changed_approval_during_inspection_rolls_back_selection_and_attempt(
    client, database, admin, authorized, change
):
    saved = await start(client, authorized)

    async def changed():
        if change in {"disabled", "reapproved"}:
            await policy_change(database, change)
        else:
            async with database() as db, db.begin():
                if change == "approver":
                    (await db.get(User, UUID(admin["id"]))).active = False
                else:
                    (
                        await db.get(ImportDestination, UUID(authorized["body"]["destination_id"]))
                    ).enabled = False

    authorized["resolver"].callback = changed
    await automatic.run(UUID(saved["id"]))
    async with database() as db:
        operation = await db.get(Operation, UUID(saved["id"]))
        assert operation.status == "held", operation.message
        assert not await db.scalar(select(AcquisitionSelection.id))
        assert not await db.scalar(select(DownloadAttempt.id))
        assert (await db.scalar(select(AcquisitionReservation))).state == "planned"
    assert authorized["qbit"].calls == []


@pytest.mark.parametrize("point", ["queued", "network-preflight"])
async def test_import_approval_is_rechecked_immediately_before_submission(
    client, database, authorized, point
):
    saved = await start(client, authorized)
    await automatic.run(UUID(saved["id"]))
    view = await detail(client, saved["id"])
    if point == "queued":
        await policy_change(database)
    else:
        authorized["qbit"].before_find = lambda: policy_change(database)
    await downloads.run(UUID(view["download_id"]))
    async with database() as db:
        attempt = await db.get(DownloadAttempt, UUID(view["download_id"]))
        assert attempt.state == "held" and not attempt.external_may_exist
        claim = await db.get(DownloadCapacity, attempt.id)
        assert not claim.slot_active and claim.submitted_at is None
    assert "submit" not in authorized["qbit"].calls


@pytest.mark.parametrize("change", ["disabled", "reapproved"])
async def test_submitted_download_still_completes_into_review_after_approval_changes(
    client, database, authorized, monkeypatch, change
):
    saved = await start(client, authorized)
    await automatic.run(UUID(saved["id"]))
    view = await detail(client, saved["id"])
    identifier = UUID(view["download_id"])
    await downloads.run(identifier)
    await policy_change(database, change)
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", False)
    qbit = authorized["qbit"]
    qbit.states = [
        s.model_copy(
            update={
                "completed": True,
                "state": "uploading",
                "progress": 1,
                "files": [f.model_copy(update={"complete": True}) for f in s.files],
            }
        )
        for s in qbit.states
    ]
    await downloads.run(identifier)
    async with database() as db:
        attempt = await db.get(DownloadAttempt, identifier)
        assert attempt.state == "complete" and attempt.inspection_id, attempt.message
        assert not await db.scalar(select(AutomaticImport.id))
    assert qbit.calls.count("submit") == 1


@pytest.mark.parametrize("failure", ["permission", "queue"])
async def test_failed_dispatch_never_leaves_an_orphan_prepared_selection(
    client, database, authorized, monkeypatch, failure
):
    saved = await start(client, authorized)
    original = downloads.enqueue

    async def fail(*args, **kwargs):
        if failure == "permission":
            raise HTTPException(409, "Synthetic dispatch authority change")
        raise RuntimeError("Synthetic queue outage")

    monkeypatch.setattr(downloads, "enqueue", fail)
    if failure == "queue":
        with pytest.raises(RuntimeError, match="queue outage"):
            await automatic.run(UUID(saved["id"]))
    else:
        await automatic.run(UUID(saved["id"]))
    async with database() as db, db.begin():
        assert not await db.scalar(select(AcquisitionSelection.id))
        assert not await db.scalar(select(DownloadAttempt.id))
        assert not await db.scalar(select(DownloadIdentityClaim.id))
        assert (await db.scalar(select(AcquisitionReservation))).state == "planned"
        operation = await db.get(Operation, UUID(saved["id"]))
        assert not operation.payload.get("selection_id")
        assert not operation.payload.get("download_id")
        if failure == "queue":
            operation.payload = {
                **operation.payload,
                "lease_until": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            }
        else:
            assert operation.status == "held"
    if failure == "queue":
        monkeypatch.setattr(downloads, "enqueue", original)
        await automatic.run(UUID(saved["id"]))
        assert (await detail(client, saved["id"]))["download_id"]


async def test_profile_changes_after_preparation_cannot_dispatch_with_old_consent(
    client, database, admin, authorized
):
    async with database() as db, db.begin():
        profile = AcquisitionProfile(
            owner_id=UUID(admin["id"]),
            name="Automatic profile",
            preferences=ReleasePreferences().model_dump(),
            generation=1,
        )
        db.add(profile)
        await db.flush()
        search = await db.get(Operation, authorized["search"])
        search.payload = {
            **search.payload,
            "profile": ProfileSnapshot(
                id=profile.id, generation=1, name=profile.name, preferences=ReleasePreferences()
            ).model_dump(mode="json"),
        }
        profile_id = profile.id
    saved = await start(client, authorized)
    await automatic.run(UUID(saved["id"]))
    view = await detail(client, saved["id"])
    async with database() as db, db.begin():
        (await db.get(AcquisitionProfile, profile_id)).generation += 1
    await downloads.run(UUID(view["download_id"]))
    assert "submit" not in authorized["qbit"].calls
    async with database() as db:
        assert (await db.get(DownloadAttempt, UUID(view["download_id"]))).state == "held"


async def test_consent_cannot_be_added_by_replaying_an_existing_preparation_command(
    client, database, authorized
):
    authorized["body"]["download_when_ready"] = False
    await start(client, authorized)
    authorized["body"]["download_when_ready"] = True
    changed = await client.post(
        "/api/acquisition/automatic-selections",
        json=authorized["body"],
        headers={"Idempotency-Key": "auto-select-fixture"},
    )
    assert changed.status_code == 409
    async with database() as db:
        assert not await db.scalar(select(DownloadAttempt.id))
