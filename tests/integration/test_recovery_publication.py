# ruff: noqa: F811
import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.db.models import (
    AssetContains,
    AuditEvent,
    ImportCapacity,
    ImportEntry,
    Library,
    LibraryAsset,
    Operation,
    RecoveryFinding,
    RestoreCheckpoint,
    Version,
    Work,
)
from app.domain import recovery_observers as observers
from app.domain import recovery_publication as recovery
from app.domain import recovery_scans as scans
from app.importing import execution
from app.jobs.queue import recovery_queue
from tests.integration.test_collection_import import collection_plan
from tests.integration.test_import_execution import (  # noqa: F401
    destination_route,
    ready_route,
)
from tests.integration.test_import_inspections import submit
from tests.integration.test_recovery_scan_workflow import begin, pause, report
from tests.integration.test_single_file_acquisition import prepare_audio_route
from tests.media_fixtures import audio, epub

pytestmark = pytest.mark.integration


def files(route):
    return {
        str(path): (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for root in (route["source"], route["target"], route["stage"])
        for path in root.rglob("*")
        if path.is_file()
    }


async def two_book_plan(client, database, route):
    epub(route["source"] / "pack/book2.epub", title="Second Harbor")
    async with database() as db, db.begin():
        work = Work(title="Second Harbor", authors=["Alex Morgan"])
        db.add(work)
        await db.flush()
        version = Version(work_id=work.id, medium="ebook")
        db.add(version)
        await db.flush()
        other_work, other_version = str(work.id), str(version.id)
    response = await submit(client, key="two-book-recovery-pack")
    from app.jobs.queue import get_queue

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
                "full_content": True,
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


@pytest.fixture
async def published(client, admin, database, ready_route, monkeypatch, request):
    route = ready_route
    scenario = getattr(request, "param", "ordinary")
    if scenario == "collection-audio":
        source = route["source"] / "pack/book.mp3"
        audio(source)
        await prepare_audio_route(
            client, database, route, route["plan"]["document"]["groups"][0]["work_id"], source
        )
    if scenario.startswith("collection"):
        route["contained"], _ = await collection_plan(client, route)
    if scenario.startswith("siblings"):
        await two_book_plan(client, database, route)
    from tests.integration.test_collection_import import start as start_any_medium

    response = await start_any_medium(client, route)
    assert response.status_code == 202, response.text
    entry = response.json()["entries"][0]

    def crash(phase):
        if phase == "published-before-receipt":
            raise RuntimeError("crash after rename, before journal and database")

    with pytest.raises(RuntimeError, match="after rename"):
        await execution.execute(UUID(entry["operation_id"]), checkpoint=crash)
    if scenario.startswith("siblings"):
        sibling = response.json()["entries"][1]

        def staged_crash(phase):
            if phase == (
                "published-before-receipt" if scenario == "siblings-published" else "prepared"
            ):
                raise RuntimeError("sibling still in staging")

        with pytest.raises(RuntimeError, match="still in staging"):
            await execution.execute(UUID(sibling["operation_id"]), checkpoint=staged_crash)
        route["sibling"] = sibling
    backend = route["scan_backend"]
    backend.scan()
    calls = []
    original = backend.handle

    async def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/libraries":
            return httpx.Response(
                200,
                json={"libraries": [{"id": "synthetic", "name": "Ebooks", "mediaType": "book"}]},
            )
        return await original(request)

    monkeypatch.setattr(backend, "handle", handle)
    monkeypatch.setattr(observers, "Audiobookshelf", backend.client)
    monkeypatch.setattr(recovery, "Audiobookshelf", backend.client)
    async with backend.client() as adapter:
        _, scope = await adapter.authorize()
    async with database() as db, db.begin():
        (await db.get(Library, UUID(route["library_id"]))).scope_fingerprint = scope
    checkpoint_id = await pause(database, admin)
    route.update(entry=entry, calls=calls, checkpoint_id=checkpoint_id)
    return route


async def observation(client, published):
    scan_id = await begin(client)
    await scans.run(UUID(scan_id))
    data = await report(client, scan_id, domain="files")
    finding = next(row for row in data["items"] if row["entity_id"] == published["entry"]["id"])
    assert finding["state"] == "published", finding
    return {"scan_id": scan_id, "finding_ids": [finding["id"]]}


async def preview(client, body, key=None):
    response = await client.post(
        "/api/recovery/publication-reconciliations",
        headers={"Idempotency-Key": key or str(uuid4())},
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def accept(client, plan, key=None):
    return await client.post(
        "/api/recovery/publication-reconciliations/" + plan["id"] + "/accept",
        headers={"Idempotency-Key": key or str(uuid4())},
        json={"revision": plan["revision"]},
    )


async def outcome(client, plan):
    response = await client.get("/api/recovery/publication-reconciliations/" + plan["id"])
    assert response.status_code == 200, response.text
    return response.json()


async def test_review_recovers_renamed_child_and_confirms_exact_abs_version_without_writes(
    client, admin, database, published
):
    before = files(published)
    body = await observation(client, published)
    plan = await preview(client, body, "same-publication-review")
    assert (await preview(client, body, "same-publication-review")) == plan
    assert plan["items"][0]["outcome"] == "confirmed", plan
    assert "connection_signature" not in str(plan) and "item_digest" not in str(plan)
    async with database() as db:
        saved = await db.get(ImportEntry, UUID(published["entry"]["id"]))
        assert saved.published_at is None and saved.confirmed_at is None
        assert (await db.get(ImportCapacity, saved.id)).resources
        queued = list(
            await db.scalars(
                text(
                    "SELECT id FROM book_queue.procrastinate_jobs "
                    "WHERE task_name NOT LIKE 'recovery.%'"
                )
            )
        )
    assert (await accept(client, plan, "same-publication-accept")).status_code == 202
    assert (await accept(client, plan, "same-publication-accept")).status_code == 202
    queue = recovery_queue()
    async with queue.open_async():
        await queue.run_worker_async(wait=False, concurrency=1)
    result = await outcome(client, plan)
    assert result["status"] == "completed", result
    assert result["results"][0]["state"] == "confirmed"
    async with database() as db:
        saved = await db.get(ImportEntry, UUID(published["entry"]["id"]))
        assert saved.state == "confirmed" and saved.published_at and saved.confirmed_at
        assert saved.run_token is None and saved.next_check_at is None
        assert saved.receipt["state"] == "prepared"  # Do not rewrite the external journal.
        asset = await db.get(LibraryAsset, saved.asset_id)
        assert asset.full_content and asset.version_id == saved.version_id
        assert all(file["import_verified"] for file in asset.files)
        assert not (await db.get(ImportCapacity, saved.id)).resources
        assert (await db.get(RestoreCheckpoint, published["checkpoint_id"])).active
        assert (await db.get(Operation, saved.operation_id)).status == "completed"
        assert queued == list(
            await db.scalars(
                text(
                    "SELECT id FROM book_queue.procrastinate_jobs "
                    "WHERE task_name NOT LIKE 'recovery.%'"
                )
            )
        )
        assert await db.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == "recovery.publication.reconciled",
                AuditEvent.entity_id == saved.id,
            )
        )
    calls = list(published["calls"])
    await recovery.run(UUID(plan["id"]))
    assert calls == published["calls"]
    assert not any(path.endswith("/scan") or "pathexists" in path for _, path in calls)
    assert files(published) == before
    assert (await client.get("/api/recovery")).json()["latest_publication_reconciliation"] == result


@pytest.mark.parametrize(
    "reason", ["abs-missing", "inode", "metadata", "scope", "withdrawn", "version"]
)
async def test_unconfirmed_child_records_publication_without_fabricating_ownership(
    client, admin, database, published, reason
):
    backend = published["scan_backend"]
    if reason == "abs-missing":
        backend.items.clear()
    elif reason == "inode":
        next(
            file
            for file in next(iter(backend.items.values()))["libraryFiles"]
            if file["metadata"]["ext"] == ".epub"
        )["ino"] = "unrelated-inode"
    elif reason == "metadata":
        next(iter(backend.items.values()))["media"]["metadata"]["title"] = "Different book"
    else:
        async with database() as db, db.begin():
            saved = await db.get(ImportEntry, UUID(published["entry"]["id"]))
            if reason == "scope":
                (await db.get(Library, UUID(published["library_id"]))).scope_fingerprint = "old"
            elif reason == "withdrawn":
                saved.state = "cancelled"
            else:
                (await db.get(Version, saved.version_id)).publication_year = 2024
    before = files(published)
    plan = await preview(client, await observation(client, published))
    wanted = "cancel-held" if reason == "withdrawn" else "awaiting-library"
    assert plan["items"][0]["outcome"] == wanted, plan
    assert (await accept(client, plan)).status_code == 202
    await recovery.run(UUID(plan["id"]))
    assert (await outcome(client, plan))["status"] == "completed"
    async with database() as db:
        saved = await db.get(ImportEntry, UUID(published["entry"]["id"]))
        assert saved.state == wanted and saved.published_at and saved.asset_id is None
        assert saved.confirmed_at is None and saved.next_check_at is None
        assert not list(await db.scalars(select(LibraryAsset)))
    assert files(published) == before


@pytest.mark.parametrize(
    "change", ["file", "journal", "backend", "scope", "root", "version", "related-finding"]
)
async def test_accepted_review_holds_changed_evidence_and_does_not_record_partial_success(
    client, admin, database, published, change
):
    plan = await preview(client, await observation(client, published))
    assert plan["items"][0]["outcome"] == "confirmed"
    assert (await accept(client, plan)).status_code == 202
    if change == "file":
        next(published["target"].rglob("*.epub")).write_bytes(b"changed after review")
    elif change == "journal":
        next(published["stage"].glob("*.json")).unlink()
    elif change == "backend":
        next(iter(published["scan_backend"].items.values()))["media"]["metadata"]["title"] = (
            "Changed"
        )
    elif change == "scope":
        published["scan_backend"].user_type = "guest"
    elif change == "root":
        published["scan_backend"].backend_path = "/another-root"
    else:
        async with database() as db, db.begin():
            saved = await db.get(ImportEntry, UUID(published["entry"]["id"]))
            if change == "version":
                (await db.get(Version, saved.version_id)).publication_year = 2024
            else:
                finding = await db.scalar(
                    select(RecoveryFinding).where(
                        RecoveryFinding.scan_id == UUID(plan["scan_id"]),
                        RecoveryFinding.state == "inventory-ready",
                    )
                )
                finding.evidence = {**finding.evidence, "inventory_digest": "changed"}
    before = files(published)
    await recovery.run(UUID(plan["id"]))
    assert (await outcome(client, plan))["status"] == "held"
    async with database() as db:
        saved = await db.get(ImportEntry, UUID(published["entry"]["id"]))
        assert saved.published_at is None and saved.confirmed_at is None
        assert not list(await db.scalars(select(LibraryAsset)))
        assert (await db.get(ImportCapacity, saved.id)).resources
    assert files(published) == before


async def test_published_file_survives_removed_original_source(client, admin, database, published):
    (published["source"] / "pack/book.epub").unlink()
    plan = await preview(client, await observation(client, published))
    assert plan["items"][0]["outcome"] == "confirmed", plan
    assert (await accept(client, plan)).status_code == 202
    await recovery.run(UUID(plan["id"]))
    assert (await outcome(client, plan))["results"][0]["state"] == "confirmed"


async def test_no_automatic_broadening_to_newly_detected_abs_item(
    client, admin, database, published
):
    published["scan_backend"].items.clear()
    plan = await preview(client, await observation(client, published))
    assert plan["items"][0]["outcome"] == "awaiting-library"
    published["scan_backend"].scan()
    assert (await accept(client, plan)).status_code == 202
    await recovery.run(UUID(plan["id"]))
    assert (await outcome(client, plan))["results"][0]["state"] == "awaiting-library"


async def test_newer_operator_state_fences_a_late_publication_worker(
    client, admin, database, published, monkeypatch
):
    plan = await preview(client, await observation(client, published))
    assert (await accept(client, plan)).status_code == 202
    started, release = asyncio.Event(), asyncio.Event()
    original = recovery.fresh_publications

    async def delayed(*args):
        result = await original(*args)
        started.set()
        await release.wait()
        return result

    monkeypatch.setattr(recovery, "fresh_publications", delayed)
    task = asyncio.create_task(recovery.run(UUID(plan["id"])))
    try:
        await asyncio.wait_for(started.wait(), 10)
        async with database() as db, db.begin():
            operation = await db.get(Operation, UUID(plan["id"]))
            operation.status = "held"
            operation.payload = {**operation.payload, "run_token": None}
    finally:
        release.set()
        await task
    async with database() as db:
        assert (await db.get(ImportEntry, UUID(published["entry"]["id"]))).published_at is None


@pytest.mark.parametrize("published", ["siblings"], indirect=True)
async def test_one_published_child_recovers_while_staged_sibling_remains_untouched(
    client, admin, database, published
):
    async with database() as db:
        sibling = await db.get(ImportEntry, UUID(published["sibling"]["id"]))
        old = {c.name: getattr(sibling, c.name) for c in ImportEntry.__table__.columns}
        reservation = dict((await db.get(ImportCapacity, sibling.id)).resources)
    before = files(published)
    body = await observation(client, published)
    data = await report(client, body["scan_id"], domain="files")
    staged = next(row for row in data["items"] if row["entity_id"] == str(sibling.id))
    assert staged["state"] == "staged"
    invalid = await client.post(
        "/api/recovery/publication-reconciliations",
        headers={"Idempotency-Key": "reject-staged-sibling"},
        json={"scan_id": body["scan_id"], "finding_ids": [staged["id"]]},
    )
    assert invalid.status_code == 409
    plan = await preview(client, body)
    assert (await accept(client, plan)).status_code == 202
    await recovery.run(UUID(plan["id"]))
    assert (await outcome(client, plan))["results"][0]["state"] == "confirmed"
    async with database() as db:
        sibling = await db.get(ImportEntry, sibling.id)
        assert old == {c.name: getattr(sibling, c.name) for c in ImportEntry.__table__.columns}
        assert (await db.get(ImportCapacity, sibling.id)).resources == reservation
    assert before == files(published)


@pytest.mark.parametrize("published", ["collection-ebook", "collection-audio"], indirect=True)
async def test_recovered_omnibus_confirms_only_its_reviewed_contents(
    client, admin, database, published
):
    plan = await preview(client, await observation(client, published))
    assert plan["items"][0]["outcome"] == "confirmed", plan
    before = files(published)
    assert (await accept(client, plan)).status_code == 202
    await recovery.run(UUID(plan["id"]))
    assert (await outcome(client, plan))["status"] == "completed"
    async with database() as db:
        saved = await db.get(ImportEntry, UUID(published["entry"]["id"]))
        asset = await db.get(LibraryAsset, saved.asset_id)
        version = await db.get(Version, saved.version_id)
        assert asset.full_content and asset.version_id == saved.version_id
        assert asset.containment["valid"]
        assert set(asset.containment["work_ids"]) == set(published["contained"])
        assert set(
            await db.scalars(
                select(AssetContains.work_id).where(
                    AssetContains.asset_id == asset.id, AssetContains.verified.is_(True)
                )
            )
        ) == {UUID(work) for work in published["contained"]} | {version.work_id}
        assert not list(
            await db.scalars(
                text(
                    "SELECT id FROM book_queue.procrastinate_jobs "
                    "WHERE task_name = 'acquisition.fulfillment' AND status = 'todo'"
                )
            )
        )
    assert files(published) == before


@pytest.mark.parametrize("published", ["siblings-published"], indirect=True)
@pytest.mark.parametrize("rollback", [False, True])
async def test_batch_records_independent_child_outcomes_atomically(
    client, admin, database, published, monkeypatch, rollback
):
    async with database() as db:
        sibling = await db.get(ImportEntry, UUID(published["sibling"]["id"]))
        folder = sibling.specification["folder"]
    backend = published["scan_backend"]
    backend.items = {
        key: value for key, value in backend.items.items() if value["path"] != "/books/" + folder
    }
    body = await observation(client, published)
    data = await report(client, body["scan_id"], domain="files")
    body["finding_ids"] = [row["id"] for row in data["items"] if row["state"] == "published"]
    plan = await preview(client, body)
    assert sorted(item["outcome"] for item in plan["items"]) == ["awaiting-library", "confirmed"]
    assert (await accept(client, plan)).status_code == 202
    before = files(published)
    if rollback:
        original = recovery.record_publication
        count = 0

        async def fail_second(*args):
            nonlocal count
            result = await original(*args)
            count += 1
            if count == 2:
                raise RuntimeError("Synthetic failure after both ledger writes")
            return result

        monkeypatch.setattr(recovery, "record_publication", fail_second)
    await recovery.run(UUID(plan["id"]))
    result = await outcome(client, plan)
    assert result["status"] == ("held" if rollback else "completed"), result
    async with database() as db:
        entries = list(await db.scalars(select(ImportEntry).order_by(ImportEntry.id)))
        if rollback:
            assert all(entry.published_at is None and entry.asset_id is None for entry in entries)
            assert all([(await db.get(ImportCapacity, entry.id)).resources for entry in entries])
            assert not list(await db.scalars(select(LibraryAsset)))
            assert not list(
                await db.scalars(
                    select(AuditEvent).where(AuditEvent.action == "recovery.publication.reconciled")
                )
            )
        else:
            assert sorted(entry.state for entry in entries) == ["awaiting-library", "confirmed"]
            assert all(entry.published_at and entry.next_check_at is None for entry in entries)
            assert sum(entry.asset_id is not None for entry in entries) == 1
    assert before == files(published)


async def test_publication_routes_require_exact_review_operator_and_command(
    client, admin, database, published
):
    body = await observation(client, published)
    plan = await preview(client, body)
    path = "/api/recovery/publication-reconciliations/" + plan["id"] + "/accept"
    wrong = await client.post(
        path, json={"revision": "0" * 64}, headers={"Idempotency-Key": "wrong-revision"}
    )
    assert wrong.status_code == 409
    csrf = await client.post(
        path,
        json={"revision": plan["revision"]},
        headers={
            "Idempotency-Key": "missing-csrf",
            "X-CSRF-Token": "incorrect",
        },
    )
    assert csrf.status_code == 403
    assert (
        await client.get("/api/recovery/inventory-reconciliations/" + plan["id"])
    ).status_code == 404
    async with httpx.AsyncClient(
        transport=client._transport, base_url="http://testserver"
    ) as anonymous:
        assert (await anonymous.get(path.removesuffix("/accept"))).status_code == 401
    duplicate = await client.post(
        "/api/recovery/publication-reconciliations",
        headers={"Idempotency-Key": "duplicate-child-finding"},
        json={**body, "finding_ids": body["finding_ids"] * 2},
    )
    assert duplicate.status_code == 422
    assert (await accept(client, plan)).status_code == 202
    assert (
        await client.post(
            "/api/recovery/scans", headers={"Idempotency-Key": "no-scan-during-publication"}
        )
    ).status_code == 409
    await recovery.run(UUID(plan["id"]))
    assert (await client.get("/api/lists")).status_code == 423
