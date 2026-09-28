# ruff: noqa: F811
import asyncio
import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.torrent_descriptor import inspect_torrent
from app.db.models import (
    CapacitySettings,
    DownloadAttempt,
    DownloadCapacity,
    FrozenImportPlan,
    ImportCapacity,
    ImportEntry,
    ImportRun,
    SourceArtifact,
    User,
)
from app.domain import capacity
from app.domain import download_attempts as downloads
from app.security import encrypt_secrets
from tests.integration.test_acquisition import catalog, request  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401
from tests.integration.test_download_attempts import downloader, selected, start  # noqa: F401
from tests.torrent_fixture import torrent_bytes

pytestmark = pytest.mark.integration
GIB = 1024**3


def observation(available=100 * GIB):
    return {
        "at": datetime.now(UTC).isoformat(),
        "roots": {"download": "disk", "library": "disk", "staging": "disk"},
        "filesystems": {"disk": {"available": available, "total": 200 * GIB}},
    }


async def limits(database, **changes):
    async with database() as db, db.begin():
        db.add(CapacitySettings(id=1, configuration={**capacity.Limits().model_dump(), **changes}))


async def another_selection(client, database, selection_route):
    work_response = await client.post(
        "/api/catalog/works",
        json={
            "title": "Another Harbor",
            "authors": ["Writer"],
        },
    )
    assert work_response.status_code == 201, work_response.text
    work = work_response.json()
    wanted = await request(client, {"work_id": work["id"], "specification": {"mode": "audio"}})
    raw = torrent_bytes(name=b"Another Harbor", files=[{b"length": 12, b"path": [b"book.m4b"]}])
    descriptor = await inspect_torrent(raw)
    async with database() as db, db.begin():
        original = await db.get(SourceArtifact, UUID(selection_route["artifact_id"]))
        artifact = SourceArtifact(
            owner_id=original.owner_id,
            source_key=original.source_key,
            source_id="another",
            source_generation=original.source_generation,
            sha256=descriptor.artifact_sha256,
            descriptor=descriptor.model_dump(mode="json"),
            encrypted_content=encrypt_secrets({"torrent": base64.b64encode(raw).decode()}),
            release_snapshot={
                **original.release_snapshot,
                "source_id": "another",
                "title": "Another Harbor",
                "raw_title": "Another Harbor",
            },
        )
        db.add(artifact)
        await db.flush()
        route = {
            **selection_route,
            "artifact_id": str(artifact.id),
            "confirmed_work_id": work["id"],
            "intent_id": wanted["request"]["id"],
        }
    response = await prepare(client, route, "second-capacity-selection")
    assert response.status_code == 201, response.text
    return response.json()


async def admit(database, identifier, storage=None):
    async with database() as db, db.begin():
        attempt, selection = await downloads.locked(db, UUID(identifier))
        try:
            await capacity.admit(db, attempt, selection, storage or observation())
            return "reserved"
        except capacity.CapacityWait as error:
            return str(error)


async def automatic_start(database, admin, selected, key):
    async with database() as db, db.begin():
        user = await db.get(User, UUID(admin["id"]))
        attempt = await downloads.start(db, user, UUID(selected["id"]), key, automatic=True)
        return str(attempt.id)


async def test_legacy_concurrent_cap_does_not_block_other_downloads(
    client, database, selected, selection_route
):
    await limits(database, active_transfers=1)
    second = await another_selection(client, database, selection_route)
    first = (await start(client, selected)).json()["id"]
    other = (await start(client, second, "second-capacity-attempt")).json()["id"]
    results = await asyncio.gather(admit(database, first), admit(database, other))
    assert results.count("reserved") == 2
    async with database() as db:
        rows = list(await db.scalars(select(DownloadCapacity)))
        assert sum(row.slot_active for row in rows) == 2
    assert (await client.delete(f"/api/acquisition/downloads/{first}")).status_code == 200
    assert await admit(database, other) == "reserved"


async def test_capacity_wait_never_contacts_downloader_and_existing_attempt_can_resume(
    client, database, selected, downloader, monkeypatch
):
    async def low(_):
        return observation(available=GIB)

    monkeypatch.setattr(capacity, "observe_download", low)
    identifier = (await start(client, selected)).json()["id"]
    await downloads.run(UUID(identifier))
    async with database() as db:
        attempt = await db.get(DownloadAttempt, UUID(identifier))
        assert attempt.state == "queued" and not attempt.external_may_exist
        assert "free disk space" in attempt.message
        assert not (await db.get(DownloadCapacity, attempt.id)).slot_active
    assert downloader.calls == []

    async def restored(_):
        return observation()

    monkeypatch.setattr(capacity, "observe_download", restored)
    await downloads.run(UUID(identifier))
    assert downloader.calls.count("submit") == 1
    await downloads.run(UUID(identifier))
    assert downloader.calls.count("submit") == 1


async def test_space_is_rechecked_after_network_preflight_before_submission(
    client, database, selected, downloader, monkeypatch
):
    calls = 0

    async def shrinking(_):
        nonlocal calls
        calls += 1
        return observation(available=100 * GIB if calls == 1 else GIB)

    monkeypatch.setattr(capacity, "observe_download", shrinking)
    identifier = (await start(client, selected)).json()["id"]
    await downloads.run(UUID(identifier))
    assert "submit" not in downloader.calls
    async with database() as db:
        attempt = await db.get(DownloadAttempt, UUID(identifier))
        pool = await db.get(DownloadCapacity, attempt.id)
        assert not attempt.external_may_exist and not pool.slot_active
        assert pool.submitted_at is None and pool.resources == {}


async def test_unknown_submission_keeps_storage_but_no_daily_cap(
    client, database, admin, selected, selection_route, downloader, monkeypatch
):
    await limits(database, active_transfers=3, automatic_per_day=1)

    async def enough(_):
        return observation()

    monkeypatch.setattr(capacity, "observe_download", enough)
    downloader.fail = AdapterError(FailureKind.TIMEOUT, "Synthetic timeout after add")
    first = await automatic_start(database, admin, selected, "automatic-capacity-one")
    await downloads.run(UUID(first))
    async with database() as db, db.begin():
        row = await db.get(DownloadCapacity, UUID(first))
        row.submitted_at = datetime.now(UTC) - timedelta(days=2)
        assert row.slot_active
        assert (await db.get(DownloadAttempt, UUID(first))).external_may_exist
    second = await another_selection(client, database, selection_route)
    identifier = await automatic_start(database, admin, second, "automatic-capacity-two")
    assert await admit(database, identifier) == "reserved"
    assert (await client.delete(f"/api/acquisition/downloads/{first}")).status_code == 409
    assert downloader.calls.count("submit") == 1


async def test_verified_download_completion_frees_slot_but_keeps_future_import_space(
    client, database, selected, downloader, monkeypatch
):
    async def enough(_):
        return observation()

    monkeypatch.setattr(capacity, "observe_download", enough)
    downloader.complete = True
    identifier = (await start(client, selected)).json()["id"]
    await downloads.run(UUID(identifier))
    async with database() as db:
        row = await db.get(DownloadCapacity, UUID(identifier))
        assert not row.slot_active and row.resources == {} and row.import_resources
        assert row.submitted_at
        assert (await db.get(DownloadAttempt, UUID(identifier))).state == "complete"


async def test_snapshot_before_another_transfer_consumes_space_cannot_spend_released_bytes(
    client, database, selected, selection_route, downloader, monkeypatch
):
    stale = observation()

    async def enough(_):
        return stale

    monkeypatch.setattr(capacity, "observe_download", enough)
    downloader.complete = True
    first = (await start(client, selected)).json()["id"]
    await downloads.run(UUID(first))
    second_selection = await another_selection(client, database, selection_route)
    second = (await start(client, second_selection, "capacity-freshness-second")).json()["id"]
    assert "accounting changed" in await admit(database, second, stale)
    fresh = observation()
    async with database() as db:
        fresh["generation"] = await capacity.storage_generation(db)
    assert fresh["generation"] > 0
    assert await admit(database, second, fresh) == "reserved"


async def test_admission_rollback_does_not_spend_capacity(client, database, selected):
    identifier = (await start(client, selected)).json()["id"]
    with pytest.raises(RuntimeError):
        async with database() as db, db.begin():
            attempt, selection = await downloads.locked(db, UUID(identifier))
            await capacity.admit(db, attempt, selection, observation())
            raise RuntimeError("rollback")
    async with database() as db:
        row = await db.get(DownloadCapacity, UUID(identifier))
        assert not row.slot_active and row.resources == {} and row.import_resources == {}


async def test_settings_enforce_privilege_validation_and_optimistic_revision(
    client, database, admin
):
    before = (await client.get("/api/acquisition/capacity")).json()
    assert "active_transfers" not in before["limits"]
    assert "automatic_per_day" not in before["limits"]
    payload = {
        "expected_revision": before["revision"],
        "limits": {**before["limits"], "minimum_free_percent": 2},
    }
    assert (await client.put("/api/acquisition/capacity", json=payload)).status_code == 200
    assert (await client.put("/api/acquisition/capacity", json=payload)).status_code == 409
    invalid = {**payload, "limits": {**payload["limits"], "minimum_free_percent": -1}}
    assert (await client.put("/api/acquisition/capacity", json=invalid)).status_code == 422
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "member"
    assert (await client.get("/api/acquisition/capacity")).status_code == 403
    assert (await client.put("/api/acquisition/capacity", json=payload)).status_code == 403


async def test_import_children_inherit_download_budget_and_release_remaining_pool_only_at_end(
    client, database, admin, catalog, selected, downloader, monkeypatch
):
    async def enough(_):
        return observation()

    monkeypatch.setattr(capacity, "observe_download", enough)
    downloader.complete = True
    identifier = UUID((await start(client, selected)).json()["id"])
    await downloads.run(identifier)
    async with database() as db, db.begin():
        attempt = await db.get(DownloadAttempt, identifier)
        pool = await db.get(DownloadCapacity, identifier)
        original = sum(pool.import_resources.values())
        plan = FrozenImportPlan(
            inspection_id=attempt.inspection_id,
            owner_id=UUID(admin["id"]),
            revision="a" * 64,
            document={},
        )
        db.add(plan)
        await db.flush()
        run = ImportRun(
            owner_id=UUID(admin["id"]), plan_id=plan.id, command_key="capacity-lineage", request={}
        )
        db.add(run)
        await db.flush()
        version_id = catalog["versions"][1]
        entries = [
            ImportEntry(
                run_id=run.id,
                group_id=uuid4(),
                version_id=version_id,
                message="Capacity-only child fixture",
            )
            for _ in range(2)
        ]
        db.add_all(entries)
        await db.flush()
        identifiers = [entry.id for entry in entries]
    storage = observation(available=10 * GIB + original)
    async with database() as db:
        storage["generation"] = await capacity.storage_generation(db)
    storage["roots"].pop("download")
    storage["required_bytes"] = 2 * capacity.MIB
    for entry_id in identifiers:
        async with database() as db, db.begin():
            await capacity.reserve_import(db, await db.get(ImportEntry, entry_id), None, storage)
            assert sum((await capacity.reserved_bytes(db)).values()) == original
    async with database() as db, db.begin():
        entry = await db.get(ImportEntry, identifiers[0])
        entry.published_at = datetime.now(UTC)
        await capacity.release_import(db, entry)
        assert (await db.get(DownloadCapacity, identifier)).import_resources
        assert not (await db.get(ImportCapacity, entry.id)).resources
    async with database() as db, db.begin():
        entry = await db.get(ImportEntry, identifiers[1])
        entry.state = "cancelled"
        await capacity.release_import(db, entry)
        assert not (await db.get(DownloadCapacity, identifier)).import_resources
        assert sum((await capacity.reserved_bytes(db)).values()) == 0


async def test_legacy_external_attempt_is_backfilled_and_blocks_new_storage_until_observed(
    client, database, selected, selection_route
):
    from app.db.session import get_engine
    from tests.integration.test_correction_migration import legacy_request_policy_fixture, migrate

    first = UUID((await start(client, selected)).json()["id"])
    second_selection = await another_selection(client, database, selection_route)
    second = (await start(client, second_selection, "capacity-migration-second")).json()["id"]
    async with database() as db, db.begin():
        row = await db.get(DownloadAttempt, first)
        row.external_may_exist, row.state = True, "downloading"
        # Explicitly create legacy state only in the disposable test database.
        await db.execute(delete(DownloadCapacity))
    await get_engine().dispose()
    try:
        await legacy_request_policy_fixture(database)
        previous = await migrate("downgrade", "0027_hardcover_lists")
        assert previous.returncode == 0, previous.stderr
        upgraded = await migrate("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        async with database() as db:
            claim = await db.get(DownloadCapacity, first)
            assert claim.slot_active and claim.submitted_at and not claim.automatic
            assert not claim.observed_mounts
        assert "storage needs reconciliation" in await admit(database, second)
        assert await admit(database, str(first)) == "reserved"
        assert await admit(database, second) == "reserved"
        await get_engine().dispose()
        await legacy_request_policy_fixture(database)
        rejected = await migrate("downgrade", "0027_hardcover_lists")
        assert rejected.returncode != 0 and "Capacity history" in rejected.stderr
    finally:
        assert (await migrate("upgrade", "head")).returncode == 0
        await get_engine().dispose()
