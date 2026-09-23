import asyncio
import threading
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db.models import (
    AuditEvent,
    DownloadInspection,
    FrozenImportPlan,
    Operation,
    ProviderObject,
    User,
    Version,
    Work,
    WorkMetadataSource,
)
from app.importing import workflow
from app.jobs.queue import enqueue, get_queue
from tests.media_fixtures import epub

pytestmark = pytest.mark.integration


@pytest.fixture
def mounted_download(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    epub(root / "pack/book.epub")
    monkeypatch.setattr(get_settings(), "import_sources", {"fixture": root})
    return root


async def submit(client, key="inspect-test", path="pack"):
    return await client.post(
        "/api/organization/inspections",
        headers={"Idempotency-Key": key},
        json={"source_key": "fixture", "relative_path": path, "completed_download": True},
    )


async def run_worker():
    await asyncio.wait_for(get_queue().run_worker_async(wait=False, concurrency=1), timeout=15)


async def test_inspection_worker_snapshot_and_frozen_plan_survive_replay(
    client, admin, database, mounted_download
):
    responses = await asyncio.gather(*(submit(client) for _ in range(4)))
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    inspection_id = responses[0].json()["id"]
    operation_id = responses[0].json()["operation_id"]
    await run_worker()
    record = (await client.get(f"/api/organization/inspections/{inspection_id}")).json()
    assert record["state"] == "ready", record
    assert record["snapshot"]["files"][0]["sha256"]
    assert (await client.get("/api/organization/inspections")).json()[0]["snapshot"] is None
    async with database() as db, db.begin():
        work = Work(title="First Harbor", authors=["Alex Morgan"])
        db.add(work)
        await db.flush()
        version = Version(work_id=work.id, medium="ebook", publication_year=2024)
        db.add(version)
        db.add(
            WorkMetadataSource(
                work_id=work.id,
                provider="hardcover",
                external_id="7",
                fetched_at=datetime.now(UTC),
                snapshot={
                    "series": [
                        {"name": "Harbor Omnibus", "position": "1-3", "compilation": True},
                        {"name": "Harbor Trilogy", "position": "1", "compilation": False},
                    ]
                },
            )
        )
        await db.flush()
        work_id, version_id = work.id, version.id
        await enqueue(db, "organization.inspect", operation_id=operation_id)
    await run_worker()
    settings = (await client.get("/api/organization/settings")).json()
    body = {
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
    }
    async with database() as db, db.begin():
        disputed = ProviderObject(
            provider="fixture",
            kind="edition",
            external_id="disputed",
            work_id=work_id,
            version_id=version_id,
            match_status="needs-review",
        )
        db.add(disputed)
        await db.flush()
        disputed_id = disputed.id
    refused = await client.post(f"/api/organization/inspections/{inspection_id}/plans", json=body)
    assert refused.status_code == 409
    async with database() as db, db.begin():
        await db.delete(await db.get(ProviderObject, disputed_id))
    plans = await asyncio.gather(
        *(
            client.post(f"/api/organization/inspections/{inspection_id}/plans", json=body)
            for _ in range(2)
        )
    )
    assert all(plan.status_code == 201 for plan in plans), [plan.text for plan in plans]
    assert plans[0].json()["id"] == plans[1].json()["id"]
    document = plans[0].json()["document"]
    assert document["files"][0]["sha256"] == record["snapshot"]["files"][0]["sha256"]
    assert document["plan"]["expected_items"] == 1
    assert document["schema_version"] == 2
    exported = document["initial_sidecars"][document["groups"][0]["id"]]
    assert set(exported) == {"metadata.opf"}
    assert document["groups"][0]["metadata"]["title"] in exported["metadata.opf"]
    metadata = document["groups"][0]["metadata"]
    assert (metadata["series"], metadata["sequence"]) == ("Harbor Trilogy", "1")
    assert metadata["part_index"] is None
    assert 'content="Harbor Trilogy"' in exported["metadata.opf"]
    assert not document["publication_available"]
    await client.put(
        "/api/organization/settings",
        json={
            "profile": {**settings["profile"], "ebook_folder": "{title}"},
            "expected_revision": settings["revision"],
        },
    )
    fetched = await client.get(f"/api/organization/plans/{plans[0].json()['id']}")
    assert fetched.json()["document"] == document
    stale = await client.post(f"/api/organization/inspections/{inspection_id}/plans", json=body)
    assert stale.status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(FrozenImportPlan)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "organization.inspected")
            )
            == 1
        )


async def test_inspection_rejects_wrong_paths_keys_and_recovery(client, admin, mounted_download):
    assert (await submit(client, path="../escape")).status_code == 422
    assert (await submit(client, path="/etc")).status_code == 422
    assert (await submit(client)).status_code == 202
    assert (await submit(client, path="different")).status_code == 409
    get_settings().import_sources.clear()
    assert (await submit(client, key="missing-root")).status_code == 422


async def test_changed_mount_or_revoked_actor_stops_queued_inspection(
    client, admin, database, mounted_download
):
    response = await submit(client)
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "member"
    await run_worker()
    assert (await client.get("/api/organization/inspections")).status_code == 403
    async with database() as db:
        row = await db.get(DownloadInspection, UUID(response.json()["id"]))
        assert row.state == "failed" and row.snapshot is None
        assert (await db.get(Operation, row.operation_id)).status == "failed"


async def test_actor_revoked_during_filesystem_work_discards_result(
    client, admin, database, mounted_download, monkeypatch
):
    started, release = threading.Event(), threading.Event()
    original = workflow.inspect_download

    def slow(*args):
        started.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(workflow, "inspect_download", slow)
    response = await submit(client)
    operation_id = UUID(response.json()["operation_id"])
    running = asyncio.create_task(workflow.run_inspection(operation_id))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        async with database() as db, db.begin():
            (await db.get(User, UUID(admin["id"]))).active = False
    finally:
        release.set()
    await running
    async with database() as db:
        row = await db.get(DownloadInspection, UUID(response.json()["id"]))
        assert row.state == "failed" and row.snapshot is None


async def test_unsafe_download_fails_without_partial_snapshot(client, admin, mounted_download):
    (mounted_download / "pack/link").symlink_to(mounted_download / "pack/book.epub")
    response = await submit(client)
    await run_worker()
    result = (await client.get(f"/api/organization/inspections/{response.json()['id']}")).json()
    assert result["state"] == "failed"
    assert result["snapshot"] is None
    assert "symlink" in result["message"]


async def test_populated_inspection_migration_blocks_destructive_downgrade(
    client, admin, database, mounted_download
):
    from app.db.session import get_engine
    from tests.integration.test_correction_migration import migrate

    await submit(client)
    await get_engine().dispose()
    try:
        result = await migrate("downgrade", "0008_organization")
        assert result.returncode != 0 and "Inspection history" in result.stderr
        async with database() as db:
            assert await db.scalar(text("SELECT count(*) FROM download_inspections")) == 1
    finally:
        assert (await migrate("upgrade", "head")).returncode == 0
        await get_engine().dispose()


async def test_superseded_inspection_attempt_cannot_overwrite_newer_result(
    client, admin, database, mounted_download, monkeypatch
):
    started, release = threading.Event(), threading.Event()
    original = workflow.inspect_download
    calls = 0

    def delayed_first(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(workflow, "inspect_download", delayed_first)
    response = await submit(client)
    operation_id = UUID(response.json()["operation_id"])
    old = asyncio.create_task(workflow.run_inspection(operation_id))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        await workflow.run_inspection(operation_id)
    finally:
        release.set()
    await old
    async with database() as db:
        row = await db.get(DownloadInspection, UUID(response.json()["id"]))
        assert row.state == "ready" and row.run_token is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "organization.inspected")
            )
            == 1
        )
