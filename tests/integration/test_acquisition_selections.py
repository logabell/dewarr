# ruff: noqa: F811
import asyncio
import base64
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.adapters.mam import release
from app.adapters.torrent_descriptor import inspect_torrent
from app.config import get_settings
from app.db.models import (
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    AssetContains,
    ImportDestination,
    Integration,
    LibraryAsset,
    Operation,
    SourceArtifact,
    SourceConnection,
    User,
    Work,
)
from app.domain.acquisition import reconcile_requests
from app.domain.work_merges import merge_works, preview_merge
from app.importing.destinations import destination_configuration
from app.importing.naming import fingerprint
from app.security import encrypt_secrets, hash_password
from tests.integration.test_acquisition import body, catalog, request  # noqa: F401
from tests.integration.test_correction_migration import legacy_request_policy_fixture, migrate
from tests.mam_fixture import release_row
from tests.torrent_fixture import torrent_bytes

pytestmark = pytest.mark.integration


@pytest.fixture
async def selection_route(client, admin, catalog, database, tmp_path, monkeypatch):
    settings = get_settings()
    for name in ("downloads", "library", "stage"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(settings, "import_sources", {"fixture": tmp_path / "downloads"})
    monkeypatch.setattr(settings, "import_destinations", {"audio": tmp_path / "library"})
    monkeypatch.setattr(settings, "import_staging_root", tmp_path / "stage")
    raw = torrent_bytes()
    descriptor = await inspect_torrent(raw)
    async with database() as db, db.begin():
        source = SourceConnection(
            key="mam",
            base_url="https://mam.test",
            encrypted_secrets=encrypt_secrets({"mam_id": "never-exposed"}),
        )
        downloader = Integration(
            kind="qbittorrent",
            name="Fixture downloader",
            base_url="http://qbit.test",
            encrypted_secrets=encrypt_secrets(
                {"username": "private-user", "password": "private-password"}
            ),
            credential_generation=1,
            status="connected",
            config={
                "save_path": "/downloads",
                "category": "book-search",
                "mappings": [
                    {
                        "download_root": "/downloads",
                        "source_key": "fixture",
                        "source_path": str(tmp_path / "downloads"),
                    },
                ],
            },
        )
        destination = ImportDestination(
            root_key="audio",
            library_id=catalog["library"],
            medium="audio",
            backend_path="/audiobooks",
        )
        db.add_all([source, downloader, destination])
        await db.flush()
        artifact = SourceArtifact(
            owner_id=UUID(admin["id"]),
            source_key="mam",
            source_id="501",
            source_generation=1,
            sha256=descriptor.artifact_sha256,
            descriptor=descriptor.model_dump(mode="json"),
            encrypted_content=encrypt_secrets({"torrent": base64.b64encode(raw).decode()}),
            release_snapshot=release(
                release_row(narrator_info='{"1":"Reader A"}'), datetime.now(UTC)
            ).model_dump(mode="json"),
        )
        db.add(artifact)
        configuration = await destination_configuration(db, destination)
        revision = fingerprint(configuration)
        # Contract fixture, not a claim of a real filesystem/ABS probe. The
        # separate import suite exercises actual probes with synthetic media.
        destination.probe = {
            "status": "verified",
            "configuration_revision": revision,
            "source_key": "fixture",
            "source_path": str(tmp_path / "downloads"),
            "hardlink": True,
            "no_replace": True,
            "backend": {"root_mapping": True},
        }
        await db.flush()
        result = {
            "artifact_id": str(artifact.id),
            "downloader_id": str(downloader.id),
            "downloader_generation": 1,
            "destination_id": str(destination.id),
            "destination_revision": revision,
            "confirmed_work_id": str(catalog["work"]),
            "slot": "audio",
        }
    saved = await request(client, body(catalog, "audio", audio_library_id=str(catalog["library"])))
    result["intent_id"] = saved["request"]["id"]
    return result


async def prepare(client, route, key="select-release-fixture"):
    return await client.post(
        "/api/acquisition/selections", json=route, headers={"Idempotency-Key": key}
    )


@pytest.mark.parametrize("dispatch_enabled", [False, True])
async def test_selection_is_immutable_idempotent_private_and_performs_no_dispatch(
    client, admin, database, selection_route, monkeypatch, dispatch_enabled
):
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", dispatch_enabled)
    responses = await asyncio.gather(*(prepare(client, selection_route) for _ in range(3)))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    selected = responses[0].json()
    assert len({r.json()["id"] for r in responses}) == 1
    assert selected["configuration_current"]
    assert selected["dispatch_available"] is dispatch_enabled
    assert (await prepare(client, selection_route, "another-identical-command")).json()[
        "id"
    ] == selected["id"]
    async with database() as db:
        row = await db.get(AcquisitionSelection, UUID(selected["id"]))
        frozen = row.frozen
        reservation = await db.get(AcquisitionReservation, row.reservation_id)
        assert reservation.state == "selected"
        assert await db.scalar(select(func.count()).select_from(AcquisitionSelection)) == 1
        assert set((await db.scalars(select(Operation.kind))).all()) == {
            "acquisition.evaluate",
            "acquisition.select",
        }
        assert not any(
            row.job_id
            for row in await db.scalars(
                select(Operation).where(Operation.kind == "acquisition.select")
            )
        )
        assert "private-password" not in str(frozen) and "private-fixture-passkey" not in str(
            frozen
        )
    await reconcile_requests()
    async with database() as db:
        assert (await db.get(AcquisitionSelection, UUID(selected["id"]))).frozen == frozen
    projected = (await client.get("/api/requests/" + selection_route["intent_id"])).json()
    assert projected["targets"][0]["source_artifact_id"] == selection_route["artifact_id"]
    for forbidden in (
        "private-password",
        "private-user",
        "never-exposed",
        "/downloads",
        "reservation_id",
    ):
        assert forbidden not in responses[0].text
    assert (
        await prepare(client, {**selection_route, "confirmed_work_id": str(uuid4())})
    ).status_code == 409
    assert (
        await prepare(
            client,
            {**selection_route, "confirmed_work_id": str(uuid4())},
            "another-identical-command",
        )
    ).status_code == 409


async def test_cancel_and_replay_cannot_reactivate_selection(
    client, admin, database, selection_route
):
    selected = (await prepare(client, selection_route)).json()
    response = await client.delete("/api/acquisition/selections/" + selected["id"])
    assert response.json()["state"] == "cancelled"
    assert (await prepare(client, selection_route)).json()["state"] == "cancelled"
    newer = await prepare(client, selection_route, "deliberate-new-selection")
    assert newer.status_code == 201 and newer.json()["id"] != selected["id"]
    history = (
        await client.get(
            "/api/acquisition/selections",
            params={"artifact_id": selection_route["artifact_id"], "limit": 1},
        )
    ).json()
    assert history["total"] == 2 and len(history["items"]) == 1


async def test_stricter_later_request_cannot_tighten_a_selected_reservation(
    client,
    admin,
    database,
    catalog,
    selection_route,
):
    selected = (await prepare(client, selection_route)).json()
    stricter = await request(
        client,
        body(
            catalog,
            "audio",
            audio_library_id=str(catalog["library"]),
            audio_version_id=str(catalog["versions"][1]),
        ),
    )
    async with database() as db:
        row = await db.get(AcquisitionSelection, UUID(selected["id"]))
        reservation = await db.get(AcquisitionReservation, row.reservation_id)
        target = await db.scalar(
            select(AcquisitionTarget).where(
                AcquisitionTarget.intent_id == UUID(stricter["request"]["id"])
            )
        )
        assert (
            target.reservation_id != reservation.id
            and reservation.requirements["version_id"] is None
        )
        assert row.frozen["requirements"] == reservation.requirements


async def test_satisfaction_or_last_reason_withdrawal_cancels_preparation(
    client,
    admin,
    database,
    catalog,
    selection_route,
):
    selected = (await prepare(client, selection_route)).json()
    saved = (await client.get("/api/requests/" + selection_route["intent_id"])).json()
    response = await client.delete(
        f"/api/requests/{saved['id']}/reasons/{saved['reasons'][0]['id']}"
    )
    assert response.status_code == 200
    assert (await client.get("/api/acquisition/selections/" + selected["id"])).json()[
        "state"
    ] == "cancelled"
    assert (await prepare(client, selection_route, "no-active-reason")).status_code == 409
    await request(client, body(catalog, "audio", audio_library_id=str(catalog["library"])))
    selected = (await prepare(client, selection_route, "active-again")).json()
    async with database() as db, db.begin():
        asset = LibraryAsset(
            library_id=catalog["library"],
            external_id="new-audio",
            version_id=catalog["versions"][1],
            medium="audio",
            state="present",
            full_content=True,
        )
        db.add(asset)
        await db.flush()
        db.add(AssetContains(asset_id=asset.id, work_id=catalog["work"], verified=True))
    await reconcile_requests()
    assert (await client.get("/api/acquisition/selections/" + selected["id"])).json()[
        "state"
    ] == "cancelled"
    assert (await prepare(client, selection_route, "already-in-library")).status_code == 409


@pytest.mark.parametrize(
    "changed", ["source", "downloader", "destination", "probe", "mounts", "recovery"]
)
async def test_configuration_changes_are_reported_and_block_new_preparation(
    client, admin, database, selection_route, monkeypatch, changed
):
    selected = (await prepare(client, selection_route)).json()
    async with database() as db, db.begin():
        if changed == "source":
            (await db.get(SourceConnection, "mam")).generation += 1
        elif changed == "downloader":
            (
                await db.get(Integration, UUID(selection_route["downloader_id"]))
            ).credential_generation += 1
        elif changed == "destination":
            (
                await db.get(ImportDestination, UUID(selection_route["destination_id"]))
            ).backend_path = "/changed"
        elif changed == "probe":
            (await db.get(ImportDestination, UUID(selection_route["destination_id"]))).probe = None
    if changed == "mounts":
        monkeypatch.setattr(get_settings(), "import_sources", {})
    if changed == "recovery":
        monkeypatch.setattr(get_settings(), "recovery_mode", True)
    assert not (await client.get("/api/acquisition/selections/" + selected["id"])).json()[
        "configuration_current"
    ]
    await client.delete("/api/acquisition/selections/" + selected["id"])
    assert (await prepare(client, selection_route, "after-config-change")).status_code == 409


@pytest.mark.parametrize(
    "field,value", [("medium", "ebook"), ("language", "fr"), ("narrators", ["Reader B"])]
)
async def test_known_source_conflicts_cannot_override_exact_request(
    client,
    admin,
    database,
    catalog,
    selection_route,
    field,
    value,
):
    saved = await request(
        client,
        body(
            catalog,
            "audio",
            audio_library_id=str(catalog["library"]),
            audio_version_id=str(catalog["versions"][1]),
            language="en",
        ),
    )
    selection_route["intent_id"] = saved["request"]["id"]
    async with database() as db, db.begin():
        artifact = await db.get(SourceArtifact, UUID(selection_route["artifact_id"]))
        artifact.release_snapshot = {**artifact.release_snapshot, field: value}
    assert (await prepare(client, selection_route)).status_code == 422


async def test_owner_isolation_and_member_options_do_not_expose_credentials(
    client, admin, database, selection_route
):
    selected = (await prepare(client, selection_route)).json()
    async with database() as db, db.begin():
        db.add(
            User(
                username="other",
                display_name="Other",
                password_hash=hash_password("other strong password"),
                role="admin",
            )
        )
    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as other:
        login = await other.post(
            "/api/auth/login", json={"username": "other", "password": "other strong password"}
        )
        other.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (await other.get("/api/acquisition/selections/" + selected["id"])).status_code == 404
        assert (
            await other.delete("/api/acquisition/selections/" + selected["id"])
        ).status_code == 404
        assert (await other.get("/api/acquisition/selections")).json()["total"] == 0
        assert (await prepare(other, selection_route)).status_code == 404
    options = await client.get("/api/acquisition/selections/options")
    assert options.json()["destinations"][0]["ready"] and options.json()["downloaders"][0]["ready"]
    assert "private" not in options.text and "base_url" not in options.text
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "viewer"
    assert (await client.get("/api/acquisition/selections/options")).status_code == 403
    assert (await prepare(client, selection_route)).status_code == 403


async def test_canonical_merge_invalidates_prepared_source_selection(
    client,
    admin,
    database,
    catalog,
    selection_route,
):
    selected = (await prepare(client, selection_route)).json()
    async with database() as db, db.begin():
        other = Work(title="Canonical harbor", authors=["Writer"])
        db.add(other)
        await db.flush()
        preview = await preview_merge(db, catalog["work"], other.id, UUID(admin["id"]))
        await merge_works(db, UUID(admin["id"]), catalog["work"], other.id, preview["revision"])
    updated = (await client.get("/api/acquisition/selections/" + selected["id"])).json()
    assert updated["state"] == "cancelled" and not updated["configuration_current"]


async def test_selection_history_prevents_lossy_downgrade(client, admin, database, selection_route):
    selected = (await prepare(client, selection_route)).json()
    await legacy_request_policy_fixture(database)
    result = await migrate("downgrade", "0016_artifacts")
    assert result.returncode and "Acquisition selection history" in result.stderr
    await client.delete("/api/acquisition/selections/" + selected["id"])
    async with database() as db, db.begin():
        await db.delete(await db.get(AcquisitionSelection, UUID(selected["id"])))
    try:
        await legacy_request_policy_fixture(database)
        result = await migrate("downgrade", "0016_artifacts")
        assert result.returncode == 0, result.stderr
    finally:
        assert (await migrate("upgrade", "head")).returncode == 0


async def test_either_allows_deliberate_alternate_medium_and_keeps_it_on_reconciliation(
    client, admin, database, catalog, selection_route
):
    async with database() as db, db.begin():
        (await db.get(LibraryAsset, catalog["asset"])).full_content = False
    saved = await request(
        client,
        body(catalog, "either", preferred_medium="ebook", audio_library_id=str(catalog["library"])),
    )
    route = {**selection_route, "intent_id": saved["request"]["id"], "slot": "either"}
    response = await prepare(client, route)
    assert response.status_code == 201, response.text
    selected = response.json()
    assert selected["medium"] == "audio"
    await reconcile_requests()
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        target = await db.get(AcquisitionTarget, selection.target_id)
        assert selection.state == "prepared" and target.reservation_id == selection.reservation_id
        assert selection.frozen["requirements"]["medium"] == "audio"


async def test_broader_request_shares_selected_requirements_without_rewriting_them(
    client, admin, database, catalog, selection_route
):
    strict = await request(
        client,
        body(
            catalog,
            "audio",
            audio_library_id=str(catalog["library"]),
            audio_version_id=str(catalog["versions"][1]),
        ),
    )
    selected = (
        await prepare(client, {**selection_route, "intent_id": strict["request"]["id"]})
    ).json()
    await reconcile_requests()
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        broad = await db.scalar(
            select(AcquisitionTarget).where(
                AcquisitionTarget.intent_id == UUID(selection_route["intent_id"])
            )
        )
        assert broad.reservation_id == selection.reservation_id
        assert selection.frozen["requirements"]["version_id"] == str(catalog["versions"][1])
    reason = strict["request"]["reasons"][0]["id"]
    await client.delete(f"/api/requests/{strict['request']['id']}/reasons/{reason}")
    await reconcile_requests()
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        assert selection.state == "cancelled"
        broad = await db.scalar(
            select(AcquisitionTarget).where(
                AcquisitionTarget.intent_id == UUID(selection_route["intent_id"])
            )
        )
        reservation = await db.get(AcquisitionReservation, broad.reservation_id)
        assert reservation.state == "planned" and reservation.requirements["version_id"] is None


async def test_recording_metadata_changes_make_selection_stale(
    client, admin, database, catalog, selection_route
):
    from app.db.models import Version

    strict = await request(
        client,
        body(
            catalog,
            "audio",
            audio_library_id=str(catalog["library"]),
            audio_version_id=str(catalog["versions"][1]),
        ),
    )
    selected = (
        await prepare(client, {**selection_route, "intent_id": strict["request"]["id"]})
    ).json()
    async with database() as db, db.begin():
        (await db.get(Version, catalog["versions"][1])).narrators = ["Corrected narrator"]
    assert not (await client.get("/api/acquisition/selections/" + selected["id"])).json()[
        "configuration_current"
    ]


async def test_private_library_grants_and_artifact_owner_are_required(
    client, admin, database, catalog, selection_route
):
    from app.db.models import LibraryGrant

    async with database() as db, db.begin():
        member_user = User(
            username="member", display_name="Member", role="member", password_hash="unused"
        )
        db.add(member_user)
        await db.flush()
        member_id = member_user.id
        actor = await db.get(User, UUID(admin["id"]))
        actor.role = "member"
    assert (await prepare(client, selection_route)).status_code == 409
    options = (await client.get("/api/acquisition/selections/options")).json()
    assert options["destinations"] == []
    async with database() as db, db.begin():
        db.add(LibraryGrant(user_id=UUID(admin["id"]), library_id=catalog["library"]))
    assert (await client.get("/api/acquisition/selections/options")).json()["destinations"][0][
        "ready"
    ]
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "admin"
        (await db.get(SourceArtifact, UUID(selection_route["artifact_id"]))).owner_id = member_id
    assert (await prepare(client, selection_route)).status_code == 404


async def test_shared_reservation_never_exposes_another_owners_artifact(
    client, admin, database, catalog, selection_route
):
    from app.db.models import LibraryGrant

    selected = (await prepare(client, selection_route)).json()
    async with database() as db, db.begin():
        user = User(
            username="shared",
            display_name="Shared member",
            role="member",
            password_hash=hash_password("shared member password"),
        )
        db.add(user)
        await db.flush()
        db.add(LibraryGrant(user_id=user.id, library_id=catalog["library"]))
    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as other:
        login = await other.post(
            "/api/auth/login", json={"username": "shared", "password": "shared member password"}
        )
        other.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        saved = await request(
            other, body(catalog, "audio", audio_library_id=str(catalog["library"]))
        )
        target = saved["request"]["targets"][0]
        assert target["source_artifact_id"] is None and "Release selected" in target["message"]
        assert (await other.get("/api/acquisition/selections/" + selected["id"])).status_code == 404
        assert (
            await other.get("/api/source-artifacts/" + selection_route["artifact_id"])
        ).status_code == 404
        async with database() as db:
            selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
            shared = await db.scalar(
                select(AcquisitionTarget).where(
                    AcquisitionTarget.intent_id == UUID(saved["request"]["id"])
                )
            )
            assert shared.reservation_id == selection.reservation_id


async def test_restored_selection_cannot_authorize_new_transfer(
    client, admin, database, selection_route, monkeypatch
):
    from app.db.models import DownloadAttempt, DownloadIdentityClaim
    from tests.integration.test_recovery_approvals import seal_history

    selected = (await prepare(client, selection_route)).json()
    await seal_history(database, admin)
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    held = (await prepare(client, selection_route)).json()
    assert not held["dispatch_available"] and not held["pack_review_available"]
    assert "predates restore" in held["message"]
    result = await client.post(
        "/api/acquisition/downloads",
        json={"selection_id": selected["id"]},
        headers={"Idempotency-Key": "restored-selection-must-not-run"},
    )
    assert result.status_code == 409 and "predates restore" in result.text
    assert (await prepare(client, selection_route)).json()["id"] == selected["id"]
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 0
        assert await db.scalar(select(func.count()).select_from(DownloadIdentityClaim)) == 0
        row = await db.get(AcquisitionSelection, UUID(selected["id"]))
        assert row.state == "prepared"
        assert (await db.get(AcquisitionReservation, row.reservation_id)).state == "selected"
