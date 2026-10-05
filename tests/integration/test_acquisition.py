import asyncio
import json
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text

from app.adapters.catalog_types import BookData
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionReservation,
    AcquisitionTarget,
    AssetContains,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    Operation,
    ProviderObject,
    User,
    Version,
    Work,
    WorkMetadataSource,
)
from app.domain.acquisition import RequestReason, RequestSpec, assess, inventory_candidates, submit
from app.jobs.queue import enqueue, get_queue
from app.security import hash_password

pytestmark = pytest.mark.integration


@pytest.fixture
async def catalog(database, admin):
    async with database() as db, db.begin():
        work = Work(title="Harbor", authors=["Writer"])
        integration = Integration(
            kind="audiobookshelf",
            name="Fixture",
            base_url="http://fixture.invalid",
            encrypted_secrets="unused",
        )
        db.add_all([work, integration])
        await db.flush()
        library = Library(
            integration_id=integration.id,
            external_id="library",
            name="Shared books",
            last_complete_sync=datetime.now(UTC),
        )
        source = WorkMetadataSource(
            work_id=work.id,
            provider="hardcover",
            external_id="42",
            fetched_at=datetime.now(UTC),
            snapshot=BookData(provider="hardcover", external_id="42", title=work.title).model_dump(
                mode="json"
            ),
        )
        db.add_all([library, source])
        await db.flush()
        versions = []
        for index, (medium, narrator, language) in enumerate(
            [
                ("ebook", [], "en"),
                ("audio", ["Reader A"], "en"),
                ("audio", ["Reader B"], "en"),
                ("audio", ["Lecteur"], "fr"),
            ]
        ):
            version = Version(
                work_id=work.id,
                medium=medium,
                title=work.title,
                narrators=narrator,
                language=language,
                abridged=False,
            )
            db.add(version)
            await db.flush()
            db.add(
                ProviderObject(
                    provider=f"hardcover:{work.id}",
                    kind="edition",
                    external_id=str(index),
                    work_id=work.id,
                    version_id=version.id,
                    metadata_source_id=source.id,
                )
            )
            versions.append(version.id)
        asset = LibraryAsset(
            library_id=library.id,
            external_id="ebook",
            version_id=versions[0],
            medium="ebook",
            state="present",
            full_content=True,
        )
        db.add(asset)
        await db.flush()
        db.add(AssetContains(asset_id=asset.id, work_id=work.id, verified=True))
        return {"work": work.id, "library": library.id, "asset": asset.id, "versions": versions}


def body(catalog, mode="both", **kwargs):
    return {"work_id": str(catalog["work"]), "specification": {"mode": mode, **kwargs}}


async def request(client, payload, key=None):
    result = await client.post(
        "/api/requests", json=payload, headers={"Idempotency-Key": key or str(uuid4())}
    )
    assert result.status_code == 202, result.text
    return result.json()


async def test_status_assessment_has_a_small_inventory_payload(
    client, admin, catalog, database, tmp_path
):
    files = [{"path": f"chapter-{n:03}.mp3", "metadata": "x" * 250} for n in range(500)]
    async with database() as db, db.begin():
        for n in range(20):
            asset = LibraryAsset(
                library_id=catalog["library"],
                external_id=f"large-recording-{n}",
                version_id=catalog["versions"][1],
                medium="audio",
                state="present",
                full_content=True,
                files=files,
                metadata_snapshot={"description": "x" * 50000},
                read_issues=["x" * 1000],
            )
            db.add(asset)
            await db.flush()
            db.add(AssetContains(asset_id=asset.id, work_id=catalog["work"], verified=True))
    async with database() as db:
        user = await db.get(User, UUID(admin["id"]))
        started = time.perf_counter()
        rows = await inventory_candidates(db, user, catalog["work"])
        elapsed = time.perf_counter() - started
        # Measure materialized data, not SQL text or driver-specific compression.
        values = [
            {
                key: value
                for key, value in (
                    entity._mapping if hasattr(entity, "_mapping") else vars(entity)
                ).items()
                if not key.startswith("_")
            }
            for asset, version, _ in rows
            for entity in (asset, version)
            if entity is not None
        ]
        loaded_bytes = len(json.dumps(values, default=str).encode())
        metrics = {
            "copies": len(rows),
            "materialized_bytes": loaded_bytes,
            "elapsed_seconds": elapsed,
        }
        (tmp_path / "inventory-payload-metrics.json").write_text(json.dumps(metrics))
        print(metrics)
        outcomes = await assess(db, user, catalog["work"], RequestSpec(mode="both"))
        assert {row["state"] for row in outcomes} == {"satisfied"}
        assert len(rows) == 21
        assert loaded_bytes < 20000, "Status polling loaded full recording manifests"


async def test_media_satisfaction_is_separate_from_work_ownership(client, admin, catalog):
    for mode, expected in [
        ("ebook", ["satisfied"]),
        ("audio", ["wanted"]),
        ("both", ["satisfied", "wanted"]),
        ("either", ["satisfied"]),
    ]:
        payload = body(catalog, mode, **({"preferred_medium": "audio"} if mode == "either" else {}))
        preview = await client.post("/api/requests/preview", json=payload)
        assert preview.status_code == 200, preview.text
        assert [target["state"] for target in preview.json()["targets"]] == expected
        assert not preview.json()["download_available"]
    work = (await client.get(f"/api/catalog/works/{catalog['work']}")).json()
    assert work["availability"]["owned"] and work["availability"]["ebook"]


async def test_concurrent_repeated_commands_share_intent_job_and_reservation(
    client, admin, catalog, database
):
    payload = body(catalog, "audio")
    results = await asyncio.gather(
        *(request(client, payload, "same-command-key") for _ in range(8))
    )
    assert len({row["request"]["id"] for row in results}) == 1
    assert len({row["operation"]["id"] for row in results}) == 1
    await asyncio.gather(*(request(client, payload) for _ in range(4)))
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 1
        assert await db.scalar(select(func.count()).select_from(AcquisitionReason)) == 1
        assert await db.scalar(select(func.count()).select_from(AcquisitionReservation)) == 1
    conflict = await client.post(
        "/api/requests", json=body(catalog, "both"), headers={"Idempotency-Key": "same-command-key"}
    )
    assert conflict.status_code == 409


async def test_atomic_rollback_and_real_worker_redelivery(database, client, admin, catalog):
    async with database() as db:
        user = await db.get(User, UUID(admin["id"]))
        await submit(
            db,
            user,
            catalog["work"],
            RequestSpec(mode="audio"),
            RequestReason(),
            "rollback-command",
        )
        await db.rollback()
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
        assert await db.scalar(text("SELECT count(*) FROM book_queue.procrastinate_jobs")) == 0
    saved = await request(client, body(catalog, "audio"))
    operation_id = UUID(saved["operation"]["id"])
    await asyncio.wait_for(get_queue().run_worker_async(wait=False, concurrency=1), 15)
    async with database() as db, db.begin():
        assert (await db.get(Operation, operation_id)).status == "completed"
        await enqueue(db, "acquisition.evaluate", operation_id=str(operation_id))
    await asyncio.wait_for(get_queue().run_worker_async(wait=False, concurrency=1), 15)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionReservation)) == 1


async def test_exact_recordings_remain_distinct_and_broad_requests_can_share(
    client, admin, catalog, database
):
    broad = await request(client, body(catalog, "audio", language="eng"))
    await request(client, body(catalog, "audio", audio_version_id=str(catalog["versions"][1])))
    await request(client, body(catalog, "audio", audio_version_id=str(catalog["versions"][2])))
    await request(client, body(catalog, "audio", audio_version_id=str(catalog["versions"][3])))
    async with database() as db:
        rows = (
            await db.scalars(
                select(AcquisitionReservation).where(AcquisitionReservation.state == "planned")
            )
        ).all()
        assert len(rows) == 3
        assert {row.requirements["version_id"] for row in rows} == {
            str(v) for v in catalog["versions"][1:]
        }
        broad_target = await db.scalar(
            select(AcquisitionTarget).where(
                AcquisitionTarget.intent_id == UUID(broad["request"]["id"])
            )
        )
        assert (
            next(row for row in rows if row.id == broad_target.reservation_id).requirements[
                "language"
            ]
            == "en"
        )


async def test_either_reuses_pending_alternate_medium(client, admin, catalog, database):
    async with database() as db, db.begin():
        (await db.get(LibraryAsset, catalog["asset"])).full_content = False
    await request(client, body(catalog, "audio"))
    await request(client, body(catalog, "either", preferred_medium="ebook"))
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionReservation)) == 1
        assert (await db.scalar(select(AcquisitionReservation))).requirements["medium"] == "audio"


@pytest.mark.parametrize(
    "state,full,expected",
    [
        ("stale", True, "awaiting-inventory"),
        ("missing-suspected", True, "awaiting-inventory"),
        ("missing-confirmed", True, "paused"),
        ("intentionally-removed", True, "paused"),
        ("present", False, "wanted"),
    ],
)
async def test_uncertain_missing_and_supplementary_assets_do_not_trigger_duplicates(
    client, admin, catalog, database, state, full, expected
):
    async with database() as db, db.begin():
        asset = await db.get(LibraryAsset, catalog["asset"])
        asset.state, asset.full_content = state, full
    preview = await client.post("/api/requests/preview", json=body(catalog, "ebook"))
    assert preview.json()["targets"][0]["state"] == expected


async def test_omnibus_coverage_does_not_satisfy_standalone_copy(client, admin, catalog, database):
    async with database() as db, db.begin():
        another = Work(title="Another book", authors=["Writer"])
        db.add(another)
        await db.flush()
        db.add(AssetContains(asset_id=catalog["asset"], work_id=another.id, verified=True))
    ordinary = await client.post("/api/requests/preview", json=body(catalog, "ebook"))
    standalone = await client.post(
        "/api/requests/preview", json=body(catalog, "ebook", standalone=True)
    )
    assert ordinary.json()["targets"][0]["state"] == "satisfied"
    assert standalone.json()["targets"][0]["state"] == "wanted"


async def test_list_removal_cancels_only_its_reason_and_retains_other_requests(
    client, admin, catalog, database
):
    manual = await request(client, body(catalog, "audio"))
    list_id = (await client.post("/api/lists", json={"name": "Curated"})).json()["id"]
    await client.post(f"/api/lists/{list_id}/entries", json={"work_id": str(catalog["work"])})
    listed = await request(client, {**body(catalog, "audio"), "reason": {"list_id": list_id}})
    assert listed["request"]["id"] == manual["request"]["id"]
    assert len(listed["request"]["reasons"]) == 2
    removed = await client.delete(f"/api/lists/{list_id}/entries/{catalog['work']}")
    assert removed.status_code == 204
    current = (await client.get(f"/api/requests/{manual['request']['id']}")).json()
    assert current["targets"][0]["state"] == "wanted"
    assert sum(reason["active"] for reason in current["reasons"]) == 1
    reason = next(reason for reason in current["reasons"] if reason["kind"] == "manual")
    cancelled = await client.delete(f"/api/requests/{current['id']}/reasons/{reason['id']}")
    assert cancelled.json()["targets"][0]["state"] == "cancelled"
    async with database() as db:
        assert (await db.scalar(select(AcquisitionReservation))).state == "released"
        assert await db.get(LibraryAsset, catalog["asset"]) is not None


async def test_cancelling_stricter_reason_relaxes_shared_planned_requirements(
    client, admin, catalog, database
):
    await request(client, body(catalog, "audio"))
    exact = await request(
        client, body(catalog, "audio", audio_version_id=str(catalog["versions"][1]))
    )
    reason = exact["request"]["reasons"][0]
    await client.delete(f"/api/requests/{exact['request']['id']}/reasons/{reason['id']}")
    async with database() as db:
        row = await db.scalar(select(AcquisitionReservation))
        assert row.state == "planned" and row.requirements["version_id"] is None


async def test_private_inventory_requests_and_destination_grants(client, admin, catalog, database):
    saved = await request(client, body(catalog, "audio"))
    async with database() as db, db.begin():
        member = User(
            username="member",
            display_name="Member",
            password_hash=hash_password("long private password"),
            role="member",
        )
        db.add(member)
        await db.flush()
        member_id = member.id
    from app.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as other:
        login = await other.post(
            "/api/auth/login", json={"username": "member", "password": "long private password"}
        )
        other.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (await other.get(f"/api/requests/{saved['request']['id']}")).status_code == 404
        assert (await other.get("/api/requests")).json()["total"] == 0
        preview = await other.post("/api/requests/preview", json=body(catalog, "ebook"))
        assert preview.json()["targets"][0]["state"] == "wanted"
        denied = await other.post(
            "/api/requests/preview",
            json=body(catalog, "audio", audio_library_id=str(catalog["library"])),
        )
        assert denied.status_code == 404
        async with database() as db, db.begin():
            db.add(LibraryGrant(user_id=member_id, library_id=catalog["library"]))
        preview = await other.post("/api/requests/preview", json=body(catalog, "ebook"))
        assert preview.json()["targets"][0]["state"] == "satisfied"
        async with database() as db, db.begin():
            (await db.get(User, member_id)).role = "viewer"
        denied = await other.post(
            "/api/requests", json=body(catalog, "audio"), headers={"Idempotency-Key": str(uuid4())}
        )
        assert denied.status_code == 403


async def test_owned_recording_does_not_satisfy_a_different_narrator(
    client, admin, catalog, database
):
    async with database() as db, db.begin():
        asset = LibraryAsset(
            library_id=catalog["library"],
            external_id="audio",
            version_id=catalog["versions"][1],
            medium="audio",
            full_content=True,
            state="present",
        )
        db.add(asset)
        await db.flush()
        db.add(AssetContains(asset_id=asset.id, work_id=catalog["work"], verified=True))
    any_audio = await client.post("/api/requests/preview", json=body(catalog, "audio"))
    other_audio = await request(
        client, body(catalog, "audio", audio_version_id=str(catalog["versions"][2]))
    )
    assert any_audio.json()["targets"][0]["state"] == "satisfied"
    assert other_audio["request"]["targets"][0]["state"] == "wanted"
    assert "Reader B" in other_audio["request"]["description"]
    work = (await client.get(f"/api/catalog/works/{catalog['work']}")).json()
    assert work["availability"]["owned"] and work["availability"]["audio"]


async def test_deleting_a_list_keeps_history_and_releases_its_reservation(
    client, admin, catalog, database
):
    list_id = (await client.post("/api/lists", json={"name": "Curated"})).json()["id"]
    await client.post(f"/api/lists/{list_id}/entries", json={"work_id": str(catalog["work"])})
    saved = await request(client, {**body(catalog, "audio"), "reason": {"list_id": list_id}})
    assert (await client.delete(f"/api/lists/{list_id}")).status_code == 204
    view = (await client.get(f"/api/requests/{saved['request']['id']}")).json()
    assert view["targets"][0]["state"] == "cancelled"
    assert view["reasons"][0]["label"] == "Former list"
    async with database() as db:
        assert (await db.scalar(select(AcquisitionReservation))).state == "released"


async def test_incompatible_version_language_cannot_join_broad_reservation(
    client, admin, catalog, database
):
    await request(client, body(catalog, "audio", language="en"))
    await request(client, body(catalog, "audio", audio_version_id=str(catalog["versions"][3])))
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionReservation)) == 2
    invalid = await client.post(
        "/api/requests/preview",
        json=body(catalog, "audio", language="en", audio_version_id=str(catalog["versions"][3])),
    )
    assert invalid.status_code == 422


async def test_current_inventory_releases_a_reservation_on_worker_recheck(
    client, admin, catalog, database
):
    saved = await request(client, body(catalog, "audio"))
    async with database() as db, db.begin():
        asset = LibraryAsset(
            library_id=catalog["library"],
            external_id="arrived",
            version_id=catalog["versions"][1],
            medium="audio",
            full_content=True,
            state="present",
        )
        db.add(asset)
        await db.flush()
        db.add(AssetContains(asset_id=asset.id, work_id=catalog["work"], verified=True))
    await asyncio.wait_for(get_queue().run_worker_async(wait=False, concurrency=1), 15)
    async with database() as db:
        assert (await db.scalar(select(AcquisitionTarget))).state == "satisfied"
        assert (await db.scalar(select(AcquisitionReservation))).state == "released"
    view = (await client.get(f"/api/requests/{saved['request']['id']}")).json()
    assert view["targets"][0]["state"] == "satisfied"


async def test_request_constraints_and_reason_validation(client, admin, catalog):
    inherited = await client.post("/api/requests/preview", json=body(catalog, "either"))
    assert inherited.status_code == 200
    assert inherited.json()["specification"]["preferred_medium"] == "audio"
    invalid = await client.post(
        "/api/requests/preview", json=body(catalog, "either", preferred_medium=None)
    )
    assert invalid.status_code == 422
    invalid = await client.post(
        "/api/requests/preview",
        json=body(catalog, "ebook", audio_version_id=str(catalog["versions"][1])),
    )
    assert invalid.status_code == 422
    invalid = await client.post(
        "/api/requests/preview",
        json={**body(catalog, "audio"), "reason": {"list_id": str(uuid4())}},
    )
    assert invalid.status_code == 404
    invalid = await client.post(
        "/api/requests/preview", json=body(catalog, "audio", audio_version_id=str(uuid4()))
    )
    assert invalid.status_code == 404


async def test_shared_destination_reuses_reservation_without_sharing_private_reasons(
    client, admin, catalog, database
):
    payload = body(catalog, "audio", audio_library_id=str(catalog["library"]))
    first = await request(client, payload)
    async with database() as db, db.begin():
        member = User(
            username="shared",
            display_name="Shared",
            role="member",
            password_hash=hash_password("shared library password"),
        )
        db.add(member)
        await db.flush()
        member_id = member.id
        db.add(LibraryGrant(user_id=member.id, library_id=catalog["library"]))
    from app.main import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as other:
        login = await other.post(
            "/api/auth/login", json={"username": "shared", "password": "shared library password"}
        )
        other.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        second = await request(other, payload)
        assert second["request"]["id"] != first["request"]["id"]
        listing = (await other.get("/api/requests")).json()
        assert listing["total"] == 1 and admin["id"] not in str(listing)
        async with database() as db:
            assert await db.scalar(select(func.count()).select_from(AcquisitionReservation)) == 1
        reason = first["request"]["reasons"][0]
        await client.delete(f"/api/requests/{first['request']['id']}/reasons/{reason['id']}")
        assert (await other.get(f"/api/requests/{second['request']['id']}")).json()["targets"][0][
            "state"
        ] == "wanted"
        async with database() as db, db.begin():
            grant = await db.get(LibraryGrant, (member_id, catalog["library"]))
            await db.delete(grant)
        assert (await other.get(f"/api/requests/{second['request']['id']}")).json()["targets"][0][
            "state"
        ] == "paused"
    await asyncio.wait_for(get_queue().run_worker_async(wait=False, concurrency=1), 15)
    async with database() as db:
        assert (await db.scalar(select(AcquisitionReservation))).state == "released"


async def test_acquisition_migration_refuses_to_discard_saved_requests(
    client, admin, catalog, database
):
    from app.db.session import get_engine
    from tests.integration.test_correction_migration import migrate

    saved = await request(client, body(catalog, "audio"))
    async with database() as db:
        current_revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
    await get_engine().dispose()
    try:
        refused = await migrate("downgrade", "0005_corrections")
        assert refused.returncode != 0
        assert "discarding request decisions" in refused.stderr
        async with database() as db:
            assert await db.get(AcquisitionIntent, UUID(saved["request"]["id"])) is not None
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version")) == current_revision
            )
    finally:
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stderr
        await get_engine().dispose()


async def test_periodic_reconciliation_repairs_completed_requests_and_permissions(
    client, admin, catalog, database
):
    from app.jobs.tasks import reconcile_acquisition

    await request(client, body(catalog, "audio"))
    await asyncio.wait_for(get_queue().run_worker_async(wait=False, concurrency=1), 15)
    async with database() as db, db.begin():
        assert (await db.scalar(select(Operation))).status == "completed"
        asset = LibraryAsset(
            library_id=catalog["library"],
            external_id="later-audio",
            version_id=catalog["versions"][1],
            medium="audio",
            full_content=True,
            state="present",
        )
        db.add(asset)
        await db.flush()
        db.add(AssetContains(asset_id=asset.id, work_id=catalog["work"], verified=True))
    await reconcile_acquisition(timestamp=1)
    async with database() as db, db.begin():
        target = await db.scalar(select(AcquisitionTarget))
        assert target.state == "satisfied" and target.reservation_id is None
        assert (await db.scalar(select(AcquisitionReservation))).state == "released"
        user = await db.get(User, UUID(admin["id"]))
        user.role = "viewer"
    await reconcile_acquisition(timestamp=2)
    async with database() as db:
        assert (await db.scalar(select(AcquisitionTarget))).state == "paused"
    await reconcile_acquisition(timestamp=2)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionReservation)) == 1


async def test_reconciliation_pages_beyond_one_batch_and_honors_recovery(
    client, admin, database, monkeypatch
):
    from app.config import get_settings
    from app.domain.acquisition import reconcile_requests

    async with database() as db, db.begin():
        for number in range(101):
            work = Work(title=f"Reconciliation fixture {number}", authors=["Example"])
            db.add(work)
            await db.flush()
            intent = AcquisitionIntent(
                owner_id=UUID(admin["id"]),
                work_id=work.id,
                fingerprint="fixture",
                specification=RequestSpec(mode="audio").model_dump(mode="json"),
            )
            db.add(intent)
            await db.flush()
            db.add(AcquisitionReason(intent_id=intent.id, kind="manual", reference="manual"))
    monkeypatch.setattr(get_settings(), "recovery_mode", True)
    await reconcile_requests()
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionTarget)) == 0
    monkeypatch.setattr(get_settings(), "recovery_mode", False)
    await reconcile_requests()
    await reconcile_requests()
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(AcquisitionTarget)) == 101
        assert await db.scalar(select(func.count()).select_from(AcquisitionReservation)) == 101
        assert set((await db.scalars(select(AcquisitionTarget.state))).all()) == {"wanted"}


async def test_replayed_command_rechecks_account_authority(client, admin, catalog, database):
    from fastapi import HTTPException

    key = str(uuid4())
    await request(client, body(catalog, "audio"), key)
    async with database() as db:
        user = await db.get(User, UUID(admin["id"]))
        async with database() as other, other.begin():
            changed = await other.get(User, user.id)
            changed.role = "viewer"
        with pytest.raises(HTTPException) as error:
            await submit(db, user, catalog["work"], RequestSpec(mode="audio"), RequestReason(), key)
        assert error.value.status_code == 403
