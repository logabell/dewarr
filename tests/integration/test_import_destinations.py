import asyncio
import threading
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    AuditEvent,
    FrozenImportPlan,
    ImportDestination,
    Integration,
    Library,
    Operation,
    Version,
    Work,
)
from app.importing import destinations
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.abs_import_fixture import ImportBackendFixture
from tests.integration.test_import_inspections import submit
from tests.media_fixtures import epub

pytestmark = pytest.mark.integration


@pytest.fixture
async def route(client, admin, database, tmp_path, monkeypatch):
    base = tmp_path.resolve()
    source, target, stage = (base / name for name in ("downloads", "library", "stage"))
    epub(source / "pack/book.epub")
    target.mkdir()
    stage.mkdir(mode=0o700)
    backend = ImportBackendFixture(target)
    monkeypatch.setattr(destinations, "Audiobookshelf", backend.client)
    settings = get_settings()
    monkeypatch.setattr(settings, "import_sources", {"fixture": source})
    monkeypatch.setattr(settings, "import_destinations", {"ebooks": target})
    monkeypatch.setattr(settings, "import_staging_root", stage)
    async with database() as db, db.begin():
        integration = Integration(
            kind="audiobookshelf",
            name="Synthetic backend",
            base_url="http://fixture",
            encrypted_secrets=encrypt_secrets({"token": "private-import-token"}),
            enabled=True,
        )
        work = Work(title="First Harbor", authors=["Alex Morgan"])
        db.add_all([integration, work])
        await db.flush()
        library = Library(integration_id=integration.id, external_id="synthetic", name="Ebooks")
        version = Version(work_id=work.id, medium="ebook")
        db.add_all([library, version])
        await db.flush()
        library_id, work_id, version_id = library.id, work.id, version.id
    result = await submit(client)
    assert result.status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    record = (await client.get(f"/api/organization/inspections/{result.json()['id']}")).json()
    settings = (await client.get("/api/organization/settings")).json()
    plan = await client.post(
        f"/api/organization/inspections/{record['id']}/plans",
        json={
            "inspection_revision": record["snapshot"]["revision"],
            "profile_revision": settings["revision"],
            "selections": [
                {
                    "group_key": record["snapshot"]["groups"][0]["key"],
                    "work_id": str(work_id),
                    "version_id": str(version_id),
                    "full_content": True,
                }
            ],
        },
    )
    assert plan.status_code == 201, plan.text
    response = await client.put(
        "/api/organization/destinations/ebooks",
        json={
            "library_id": str(library_id),
            "medium": "ebook",
            "backend_path": "/books",
        },
    )
    assert response.status_code == 200, response.text
    return {
        "backend": backend,
        "destination": response.json(),
        "plan_id": plan.json()["id"],
        "source": source,
        "target": target,
        "stage": stage,
        "library_id": str(library_id),
    }


async def start_probe(client, route, key="test-destination"):
    return await client.post(
        f"/api/organization/destinations/{route['destination']['id']}/probe",
        headers={"Idempotency-Key": key},
        json={
            "plan_id": route["plan_id"],
            "expected_revision": route["destination"]["revision"],
        },
    )


async def test_real_destination_probe_and_idempotent_dispatch(client, admin, database, route):
    responses = await asyncio.gather(*(start_probe(client, route) for _ in range(3)))
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    await get_queue().run_worker_async(wait=False, concurrency=1)
    view = (await client.get("/api/organization/destinations")).json()[0]
    assert view["probe"]["status"] == "verified"
    assert view["probe"]["hardlink"] and view["probe"]["no_replace"]
    assert view["publication_available"]
    assert not list(route["target"].iterdir()) and not list(route["stage"].iterdir())
    async with database() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "organization.destination.probed")
            )
            == 1
        )


async def test_stale_edits_and_source_configuration_invalidate_probe(client, admin, route):
    await start_probe(client, route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    bad = await client.put(
        "/api/organization/destinations/ebooks",
        json={
            "library_id": route["library_id"],
            "medium": "ebook",
            "backend_path": "/changed",
        },
    )
    assert bad.status_code == 409
    get_settings().import_sources["fixture"] = route["source"].parent / "different"
    view = (await client.get("/api/organization/destinations")).json()[0]
    assert view["probe"] is None
    assert (await start_probe(client, route, key="new-source-path")).status_code == 409


async def test_changed_backend_credentials_invalidate_recorded_mapping(
    client, admin, database, route
):
    await start_probe(client, route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert (await client.get("/api/organization/destinations")).json()[0]["probe"][
        "status"
    ] == "verified"
    async with database() as db, db.begin():
        library = await db.get(Library, UUID(route["library_id"]))
        integration = await db.get(Integration, library.integration_id)
        integration.credential_generation += 1
    current = (await client.get("/api/organization/destinations")).json()[0]
    assert current["probe"] is None and current["revision"] != route["destination"]["revision"]
    assert (await start_probe(client, route, key="changed-credentials")).status_code == 409


async def test_credential_change_during_remote_challenge_discards_result(
    client, admin, database, route
):
    changed = False

    async def rotate(name):
        nonlocal changed
        if (route["target"] / name).exists() and not changed:
            async with database() as db, db.begin():
                library = await db.get(Library, UUID(route["library_id"]))
                integration = await db.get(Integration, library.integration_id)
                integration.credential_generation += 1
            changed = True

    route["backend"].before_exists = rotate
    response = await start_probe(client, route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert changed and not list(route["target"].iterdir())
    assert (await client.get("/api/organization/destinations")).json()[0]["probe"] is None
    async with database() as db:
        operation = await db.get(Operation, UUID(response.json()["id"]))
        assert operation.status == "failed" and "discarded" in operation.message


async def test_wrong_backend_mount_is_a_durable_actionable_failure(client, admin, route, tmp_path):
    different = tmp_path.resolve() / "wrong-mount"
    different.mkdir()
    route["backend"].root = different
    await start_probe(client, route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    report = (await client.get("/api/organization/destinations")).json()[0]["probe"]
    assert report["status"] == "failed" and "same library folder" in report["message"]
    assert not list(route["target"].iterdir()) and not list(route["stage"].iterdir())


async def test_unreadable_backend_secret_finishes_with_repair_message(
    client, admin, database, route
):
    response = await start_probe(client, route)
    async with database() as db, db.begin():
        library = await db.get(Library, UUID(route["library_id"]))
        integration = await db.get(Integration, library.integration_id)
        integration.encrypted_secrets = "invalid-encrypted-fixture"
    await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        operation = await db.get(Operation, UUID(response.json()["id"]))
        assert operation.status == "failed" and "save the connection again" in operation.message
    assert not route["backend"].path_checks and not list(route["target"].iterdir())


async def test_destination_edit_during_probe_discards_stale_evidence(
    client, admin, database, route, monkeypatch
):
    started, release = threading.Event(), threading.Event()
    original = destinations.probe_destination

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(destinations, "probe_destination", slow)
    response = await start_probe(client, route)
    pending = asyncio.create_task(destinations.probe_route(UUID(response.json()["id"])))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        edited = await client.put(
            "/api/organization/destinations/ebooks",
            json={
                "library_id": route["library_id"],
                "medium": "ebook",
                "backend_path": "/changed",
                "expected_revision": route["destination"]["revision"],
            },
        )
        assert edited.status_code == 200
    finally:
        release.set()
    await pending
    assert (await client.get("/api/organization/destinations")).json()[0]["probe"] is None
    async with database() as db:
        assert (await db.get(Operation, UUID(response.json()["id"]))).status == "failed"


async def test_duplicate_worker_attempt_cannot_fail_a_newer_completed_probe(
    client, admin, database, route, monkeypatch
):
    started, release = threading.Event(), threading.Event()
    calls = 0
    original = destinations.probe_destination

    def slow_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(destinations, "probe_destination", slow_first)
    response = await start_probe(client, route)
    op_id = UUID(response.json()["id"])
    pending = asyncio.create_task(destinations.probe_route(op_id))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        await destinations.probe_route(op_id)
    finally:
        release.set()
    await pending
    async with database() as db:
        assert (await db.get(Operation, op_id)).status == "completed"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "organization.destination.probed")
            )
            == 1
        )


async def test_unavailable_library_rejects_probes_and_unsafe_backend_paths(
    client, admin, database, route
):
    for path in ["relative", "/../escape", "/books//bad"]:
        response = await client.put(
            "/api/organization/destinations/ebooks",
            json={
                "library_id": route["library_id"],
                "medium": "ebook",
                "backend_path": path,
                "expected_revision": route["destination"]["revision"],
            },
        )
        assert response.status_code == 422
    async with database() as db, db.begin():
        (await db.get(Library, UUID(route["library_id"]))).accessible = False
    assert (await start_probe(client, route)).status_code == 409


async def test_destination_downgrade_guard(client, admin, database, route):
    from app.db.session import get_engine
    from tests.integration.test_correction_migration import migrate

    await get_engine().dispose()
    try:
        refused = await migrate("downgrade", "0009_import_plans")
        assert refused.returncode != 0 and "Destination configuration" in refused.stderr
        async with database() as db:
            assert await db.get(ImportDestination, UUID(route["destination"]["id"])) is not None
            assert await db.get(FrozenImportPlan, UUID(route["plan_id"])) is not None
    finally:
        assert (await migrate("upgrade", "head")).returncode == 0
        await get_engine().dispose()


@pytest.mark.parametrize("legacy_reference", [False, True])
async def test_restored_route_requires_fresh_probe_before_automatic_import_approval(
    client, admin, database, route, legacy_reference
):
    from tests.integration.test_recovery_approvals import seal_history

    await start_probe(client, route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    identifier = UUID(route["destination"]["id"])
    async with database() as db, db.begin():
        row = await db.get(ImportDestination, identifier)
        saved_probe, saved_operation = row.probe, row.probe_operation_id
        if legacy_reference:
            row.probe_operation_id = None
    assert saved_probe["status"] == "verified"
    await seal_history(database, admin)
    current = (await client.get("/api/organization/destinations")).json()[0]
    assert current["probe"] is None and not current["publication_available"]
    body = {"enabled": True, "expected_generation": 0, "destination_revision": current["revision"]}
    endpoint = f"/api/organization/destinations/{identifier}/automatic-import"
    rejected = await client.put(endpoint, json=body)
    assert rejected.status_code == 409, rejected.text
    async with database() as db:
        assert (await db.get(ImportDestination, identifier)).probe == saved_probe
    fresh = await start_probe(client, route, key="fresh-route-after-recovery")
    assert fresh.status_code == 202, fresh.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    current = (await client.get("/api/organization/destinations")).json()[0]
    assert current["publication_available"]
    approved = await client.put(endpoint, json=body)
    assert approved.status_code == 200 and approved.json()["ready"], approved.text
    async with database() as db:
        assert (await db.get(ImportDestination, identifier)).probe_operation_id != saved_operation
