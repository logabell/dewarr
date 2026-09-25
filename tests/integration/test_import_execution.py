import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    ImportEntry,
    ImportRun,
    Integration,
    Library,
    LibraryAsset,
    Operation,
    User,
    Version,
    Work,
)
from app.importing import execution
from app.jobs.queue import get_queue
from app.jobs.tasks import schedule_import_confirmation
from tests.abs_import_fixture import ScanningBackend
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_destinations import start_probe
from tests.integration.test_import_inspections import submit
from tests.media_fixtures import epub

pytestmark = pytest.mark.integration


@pytest.fixture
async def ready_route(client, admin, destination_route, monkeypatch):  # noqa: F811
    route = destination_route
    await start_probe(client, route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    backend = ScanningBackend(route["target"])
    monkeypatch.setattr(execution, "Audiobookshelf", backend.client)
    route["scan_backend"] = backend
    route["plan"] = (await client.get(f"/api/organization/plans/{route['plan_id']}")).json()
    return route


async def start(client, route, key="import-fixture"):
    return await client.post(
        f"/api/organization/plans/{route['plan_id']}/imports",
        headers={"Idempotency-Key": key},
        json={
            "plan_revision": route["plan"]["revision"],
            "destinations": {
                "ebook": {
                    "id": route["destination"]["id"],
                    "revision": route["destination"]["revision"],
                }
            },
        },
    )


async def test_full_publication_preserves_source_and_confirms_exact_version(
    client, admin, database, ready_route
):
    route = ready_route
    original = (route["source"] / "pack/book.epub").read_bytes()
    responses = await asyncio.gather(*(start(client, route) for _ in range(3)))
    assert all(response.status_code == 202 for response in responses), [r.text for r in responses]
    assert len({response.json()["id"] for response in responses}) == 1
    run = responses[0].json()
    await get_queue().run_worker_async(wait=False, concurrency=1)
    updated = (await client.get(f"/api/organization/imports/{run['id']}")).json()
    assert updated["entries"][0]["state"] == "confirmed", updated
    assert (route["source"] / "pack/book.epub").read_bytes() == original
    imported = list(route["target"].rglob("*.epub"))
    assert (
        len(imported) == 1
        and imported[0].stat().st_ino == (route["source"] / "pack/book.epub").stat().st_ino
    )
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(ImportRun)) == 1
        asset = await db.get(LibraryAsset, UUID(updated["entries"][0]["asset_id"]))
        assert asset.full_content and str(asset.version_id) == updated["entries"][0]["version_id"]
    repeated = await start(client, route, "another-import-command")
    assert repeated.json()["entries"][0]["state"] == "skipped"


async def test_waiting_for_backend_never_claims_ownership_or_republishes(
    client, admin, database, ready_route
):
    route = ready_route
    route["scan_backend"].detect = False
    result = await start(client, route)
    assert result.status_code == 202, result.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    current = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()[
        "entries"
    ][0]
    assert current["state"] == "awaiting-library" and current["asset_id"] is None
    original_inode = next(route["target"].rglob("*.epub")).stat().st_ino
    (route["source"] / "pack/book.epub").unlink()
    route["scan_backend"].detect = True
    route["scan_backend"].scan()
    await execution.execute(UUID(current["operation_id"]))
    updated = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()[
        "entries"
    ][0]
    assert updated["state"] == "confirmed", updated
    assert next(route["target"].rglob("*.epub")).stat().st_ino == original_inode


async def test_crash_after_filesystem_publication_recovers_one_item(
    client, admin, database, ready_route
):
    result = await start(client, ready_route)
    entry = result.json()["entries"][0]

    def crash(phase):
        if phase == "published-before-database":
            raise RuntimeError("Synthetic process crash")

    with pytest.raises(RuntimeError, match="process crash"):
        await execution.execute(UUID(entry["operation_id"]), checkpoint=crash)
    assert len(list(ready_route["target"].rglob("*.epub"))) == 1

    await execution.execute(UUID(entry["operation_id"]))
    async with database() as db:
        current = await db.get(ImportEntry, UUID(entry["id"]))
        assert current.state == "confirmed"
        assert (await db.get(Operation, current.operation_id)).status == "completed"
    assert len(list(ready_route["target"].rglob("*.epub"))) == 1


@pytest.mark.parametrize("change", ["permission", "version"])
async def test_final_guard_blocks_changed_authority_or_identity(
    client, admin, database, ready_route, change
):
    result = await start(client, ready_route)
    entry = result.json()["entries"][0]
    loop = asyncio.get_running_loop()

    async def mutate():
        async with database() as db, db.begin():
            if change == "permission":
                (await db.get(User, UUID(admin["id"]))).active = False
            else:
                (await db.get(Version, UUID(entry["version_id"]))).publication_year = 2025

    def checkpoint(phase):
        if phase == "prepared":
            asyncio.run_coroutine_threadsafe(mutate(), loop).result()

    await execution.execute(UUID(entry["operation_id"]), checkpoint=checkpoint)
    async with database() as db:
        assert (await db.get(ImportEntry, UUID(entry["id"]))).state == "held"
    assert not list(ready_route["target"].rglob("*.epub"))


async def test_overlapping_commands_reserve_one_version(client, admin, database, ready_route):
    results = await asyncio.gather(
        *(start(client, ready_route, f"overlap-{index}") for index in range(2))
    )
    assert sorted(result.json()["entries"][0]["state"] for result in results) == ["held", "queued"]
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert len(list(ready_route["target"].rglob("*.epub"))) == 1


async def test_multiple_bindings_to_same_library_share_version_reservation(
    client, admin, database, ready_route
):
    route = ready_route
    get_settings().import_destinations["also-ebooks"] = route["target"]
    second = await client.put(
        "/api/organization/destinations/also-ebooks",
        json={"library_id": route["library_id"], "medium": "ebook", "backend_path": "/books"},
    )
    assert second.status_code == 200, second.text
    current = (await client.get("/api/organization/destinations")).json()
    routes = [{**route, "destination": destination} for destination in current]
    for index, selected in enumerate(routes):
        probe = await start_probe(client, selected, key=f"shared-library-probe-{index}")
        assert probe.status_code == 202, probe.text
        await get_queue().run_worker_async(wait=False, concurrency=1)
    results = await asyncio.gather(
        *(
            start(client, selected, f"shared-library-{index}")
            for index, selected in enumerate(routes)
        )
    )
    assert all(result.status_code == 202 for result in results), [r.text for r in results]
    assert sorted(result.json()["entries"][0]["state"] for result in results) == ["held", "queued"]


@pytest.mark.parametrize("change", ["metadata", "media"])
async def test_backend_mismatch_holds_import_without_claiming_ownership(
    client, admin, database, ready_route, monkeypatch, change
):
    backend = ready_route["scan_backend"]
    original_scan = backend.scan

    def scan():
        original_scan()
        for item in backend.items.values():
            if change == "metadata":
                item["media"]["metadata"]["title"] = "A different book"
            else:
                item["libraryFiles"][0]["metadata"]["size"] += 1

    monkeypatch.setattr(backend, "scan", scan)
    result = await start(client, ready_route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        entry = await db.get(ImportEntry, UUID(result.json()["entries"][0]["id"]))
        assert entry.state == "held" and entry.published_at is not None and entry.asset_id is None
        assert await db.scalar(select(func.count()).select_from(LibraryAsset)) == 0


async def test_periodic_confirmation_requeues_once_without_republishing(
    client, admin, database, ready_route
):
    ready_route["scan_backend"].detect = False
    result = await start(client, ready_route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    entry_id = UUID(result.json()["entries"][0]["id"])
    inode = next(ready_route["target"].rglob("*.epub")).stat().st_ino
    async with database() as db, db.begin():
        entry = await db.get(ImportEntry, entry_id)
        entry.next_check_at = datetime.now(UTC) - timedelta(minutes=1)
        previous_job = (await db.get(Operation, entry.operation_id)).job_id
    await asyncio.gather(*(schedule_import_confirmation(0) for _ in range(2)))
    async with database() as db:
        entry = await db.get(ImportEntry, entry_id)
        queued_job = (await db.get(Operation, entry.operation_id)).job_id
        assert queued_job != previous_job
    retry = await client.post(
        f"/api/organization/imports/{result.json()['id']}/entries/{entry_id}/retry"
    )
    assert retry.status_code == 202, retry.text
    async with database() as db:
        entry = await db.get(ImportEntry, entry_id)
        assert (await db.get(Operation, entry.operation_id)).job_id == queued_job
    ready_route["scan_backend"].detect = True
    await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        assert (await db.get(ImportEntry, entry_id)).state == "confirmed"
    assert next(ready_route["target"].rglob("*.epub")).stat().st_ino == inode


async def test_explicit_retry_accepts_rotated_credentials_for_same_frozen_route(
    client, admin, database, ready_route
):
    ready_route["scan_backend"].detect = False
    result = await start(client, ready_route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    entry_id = UUID(result.json()["entries"][0]["id"])
    async with database() as db, db.begin():
        library = await db.get(Library, UUID(ready_route["library_id"]))
        integration = await db.get(Integration, library.integration_id)
        integration.credential_generation += 1
    retry = await client.post(
        f"/api/organization/imports/{result.json()['id']}/entries/{entry_id}/retry"
    )
    assert retry.status_code == 202, retry.text
    ready_route["scan_backend"].detect = True
    await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        assert (await db.get(ImportEntry, entry_id)).state == "confirmed"


async def test_unrelated_destination_is_preserved_and_held(client, admin, database, ready_route):
    result = await start(client, ready_route)
    entry = result.json()["entries"][0]
    async with database() as db:
        row = await db.get(ImportEntry, UUID(entry["id"]))
        folder = ready_route["target"] / row.specification["folder"]
    folder.mkdir(parents=True)
    original = folder / "existing.txt"
    original.write_text("Keep existing library content")
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert original.read_text() == "Keep existing library content"
    async with database() as db:
        assert (await db.get(ImportEntry, UUID(entry["id"]))).state == "held"
    assert not list(ready_route["target"].rglob("*.epub"))


async def test_resolved_pack_child_imports_while_unverified_child_stays_held(
    client, admin, database, ready_route
):
    route = ready_route
    epub(route["source"] / "pack/book2.epub", title="Second Harbor")
    async with database() as db, db.begin():
        work = Work(title="Second Harbor", authors=["Alex Morgan"])
        db.add(work)
        await db.flush()
        version = Version(work_id=work.id, medium="ebook")
        db.add(version)
        await db.flush()
        other_work, other_version = str(work.id), str(version.id)
    response = await submit(client, key="two-book-pack")
    await get_queue().run_worker_async(wait=False, concurrency=1)
    inspection = (await client.get(f"/api/organization/inspections/{response.json()['id']}")).json()
    naming = (await client.get("/api/organization/settings")).json()
    original = route["plan"]["document"]["groups"][0]
    selections = []
    for group in inspection["snapshot"]["groups"]:
        second = group["files"][0]["path"] == "book2.epub"
        selections.append(
            {
                "group_key": group["key"],
                "work_id": other_work if second else original["work_id"],
                "version_id": other_version if second else original["version_id"],
                "full_content": not second,
            }
        )
    plan = await client.post(
        f"/api/organization/inspections/{inspection['id']}/plans",
        json={
            "inspection_revision": inspection["snapshot"]["revision"],
            "profile_revision": naming["revision"],
            "selections": selections,
        },
    )
    assert plan.status_code == 201, plan.text
    route["plan"], route["plan_id"] = plan.json(), plan.json()["id"]
    result = await start(client, route)
    assert result.status_code == 202, result.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    current = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()
    assert sorted(entry["state"] for entry in current["entries"]) == ["confirmed", "held"]
    assert len(list(route["target"].rglob("*.epub"))) == 1
    assert (route["source"] / "pack/book2.epub").is_file()


async def test_populated_import_history_blocks_destructive_downgrade(
    client, admin, database, ready_route
):
    from app.db.session import get_engine
    from tests.integration.test_correction_migration import migrate

    await start(client, ready_route)
    await get_engine().dispose()
    try:
        result = await migrate("downgrade", "0010_destinations")
        assert result.returncode != 0 and "Published import history" in result.stderr
        async with database() as db:
            assert await db.scalar(select(func.count()).select_from(ImportRun)) == 1
    finally:
        assert (await migrate("upgrade", "head")).returncode == 0
        await get_engine().dispose()


@pytest.mark.parametrize("prior_start", [False, True])
async def test_restored_plan_cannot_publish_under_a_fresh_command(
    client, admin, database, ready_route, prior_start
):
    from tests.integration.test_recovery_approvals import seal_history

    original = ready_route["source"] / "pack/book.epub"
    evidence = (original.read_bytes(), original.stat().st_ino, original.stat().st_mtime_ns)
    receipt = (await start(client, ready_route)).json() if prior_start else None
    await seal_history(database, admin)
    if receipt:
        replay = await start(client, ready_route)
        assert replay.status_code == 202 and replay.json()["id"] == receipt["id"]
        entry = replay.json()["entries"][0]
        assert not entry["can_retry"] and not entry["can_cancel"]
        cancelled = await client.post(
            f"/api/organization/imports/{receipt['id']}/entries/{entry['id']}/cancel"
        )
        assert cancelled.status_code == 409 and "predates restore" in cancelled.text
    rejected = await start(client, ready_route, key="new-command-after-restore")
    assert rejected.status_code == 409 and "predates restore" in rejected.text
    assert (original.read_bytes(), original.stat().st_ino, original.stat().st_mtime_ns) == evidence
    assert not list(ready_route["target"].rglob("*.epub"))
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(ImportRun)) == int(prior_start)


async def test_persisted_library_mount_publishes_without_destination_environment(
    client, admin, database, ready_route, monkeypatch
):
    from app.db.models import ImportStorageSettings

    async with database() as db, db.begin():
        db.add(
            ImportStorageSettings(
                id=1,
                destinations={"ebooks": str(ready_route["target"])},
                sources={},
                staging_root=str(ready_route["stage"]),
            )
        )
    monkeypatch.setattr(get_settings(), "import_destinations", {})
    monkeypatch.setattr(get_settings(), "import_staging_root", None)
    original = ready_route["source"] / "pack/book.epub"
    before = original.read_bytes()
    response = await start(client, ready_route)
    assert response.status_code == 202, response.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    run = (await client.get(f"/api/organization/imports/{response.json()['id']}")).json()
    assert run["entries"][0]["state"] == "confirmed", run
    published = next(ready_route["target"].rglob("*.epub"))
    assert published.stat().st_ino == original.stat().st_ino
    assert original.read_bytes() == before


async def test_retry_rebinds_staging_after_filesystem_identity_change(
    client, admin, database, ready_route
):
    result = await start(client, ready_route)
    run = result.json()
    entry = run["entries"][0]

    def interrupted(phase):
        if phase == "prepared":
            raise execution.PublicationError("Staged item identity changed")

    await execution.execute(UUID(entry["operation_id"]), checkpoint=interrupted)
    # Complete the queued job just as the worker does after recording a held import.
    await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        held = await db.get(ImportEntry, UUID(entry["id"]))
        assert held.state == "held"
        journal = Path(held.specification["staging_root"]) / f"{held.id}.json"
    receipt = json.loads(journal.read_text())
    receipt["stage_identity"]["inode"] += 100
    journal.write_text(json.dumps(receipt))
    retried = await client.post(
        f"/api/organization/imports/{run['id']}/entries/{entry['id']}/retry"
    )
    assert retried.status_code == 202, retried.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    current = (await client.get(f"/api/organization/imports/{run['id']}")).json()["entries"][0]
    assert current["state"] == "confirmed", current["message"]
    original = ready_route["source"] / "pack/book.epub"
    published = list(ready_route["target"].rglob("*.epub"))
    assert len(published) == 1 and published[0].stat().st_ino == original.stat().st_ino
    assert (
        await client.get(f"/api/organization/inspections/{ready_route['plan']['inspection_id']}")
    ).json()["plan_id"] == run["plan_id"]
