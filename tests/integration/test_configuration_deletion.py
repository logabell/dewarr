# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.models import (
    AcquisitionDefaults,
    AcquisitionSelection,
    AutomaticImportPolicy,
    DownloadAttempt,
    ImportDestination,
    Integration,
    Library,
    Operation,
    SourceConnection,
    User,
)
from app.security import decrypt_secrets
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401

pytestmark = pytest.mark.integration


async def downloader(client, **changes):
    response = await client.post(
        "/api/downloaders",
        json={
            "kind": "qbittorrent",
            "name": "Delete me",
            "base_url": "http://qbit.test",
            "password": "private-password",
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_delete_downloader_clears_secrets_defaults_and_allows_reconnect(
    client, admin, database
):
    saved = await downloader(client)
    async with database() as db, db.begin():
        db.add(
            AcquisitionDefaults(
                key="installation",
                preferences={
                    "downloader_id": saved["id"],
                    "language": "en",
                },
            )
        )
    url = f"/api/downloaders/{saved['id']}"
    assert (await client.delete(url, params={"expected_generation": 0})).status_code == 409
    assert (
        await client.delete(url, params={"expected_generation": saved["generation"]})
    ).status_code == 204
    assert (await client.get("/api/downloaders")).json() == []
    assert (await client.get("/api/setup/readiness")).json()["downloaders"] == []
    assert (await client.post(url + "/test")).status_code == 404
    assert (
        await client.delete(url, params={"expected_generation": saved["generation"]})
    ).status_code == 404
    async with database() as db:
        row = await db.get(Integration, UUID(saved["id"]))
        assert row.deleted_at and not row.enabled
        assert decrypt_secrets(row.encrypted_secrets) == {}
        assert (await db.get(AcquisitionDefaults, "installation")).preferences == {"language": "en"}
    replacement = await downloader(client)
    assert replacement["id"] != saved["id"]


@pytest.mark.parametrize(
    "source,body",
    [
        ("mam", {"base_url": "https://www.myanonamouse.net", "mam_id": "test-cookie"}),
        ("prowlarr", {"base_url": "http://prowlarr.test", "api_key": "private-key"}),
        ("audiobookbay", {"base_url": "https://audiobookbay.lu"}),
        ("slskd", {"base_url": "http://slskd.test", "api_key": "private-key-long-enough"}),
    ],
)
async def test_sources_delete_reconnect_preserves_generation_and_cooldown(
    client,
    admin,
    database,
    source,
    body,
):
    url = f"/api/sources/{source}/connection"
    saved = await client.put(url, json=body)
    assert saved.status_code == 200, saved.text
    generation = saved.json()["generation"]
    until = datetime.now(UTC) + timedelta(minutes=5)
    async with database() as db, db.begin():
        row = await db.get(SourceConnection, source)
        row.blocked_until = until
    assert (
        await client.delete(url, params={"expected_generation": generation - 1})
    ).status_code == 409
    assert (await client.delete(url, params={"expected_generation": generation})).status_code == 204
    removed = (await client.get(url)).json()
    assert not removed["configured"] and not removed["enabled"]
    assert removed["generation"] > generation
    if source == "slskd":
        assert (await client.get("/api/downloaders")).json() == []
    async with database() as db:
        row = await db.get(SourceConnection, source)
        assert row.blocked_until == until
        assert decrypt_secrets(row.encrypted_secrets) == {}
    stale = await client.put(url, json={**body, "expected_generation": generation})
    assert stale.status_code == 409
    restored = await client.put(url, json={**body, "expected_generation": removed["generation"]})
    assert restored.status_code == 200, restored.text
    assert restored.json()["configured"]
    assert restored.json()["generation"] > removed["generation"]


async def test_deleting_soulseek_downloader_also_removes_source(client, admin):
    saved = (
        await client.put(
            "/api/sources/slskd/connection",
            json={
                "base_url": "http://slskd.test",
                "api_key": "a-long-private-key",
            },
        )
    ).json()
    response = await client.delete(
        f"/api/downloaders/{saved['downloader_id']}",
        params={
            "expected_generation": saved["downloader_generation"],
        },
    )
    assert response.status_code == 204, response.text
    assert not (await client.get("/api/sources/slskd/connection")).json()["configured"]


@pytest.mark.parametrize("kind", ["audiobookshelf", "grimmory"])
async def test_library_deletion_retires_folders_and_preserves_history(
    client,
    admin,
    database,
    selection_route,
    kind,
):
    async with database() as db, db.begin():
        destination = await db.get(ImportDestination, UUID(selection_route["destination_id"]))
        library = await db.get(Library, destination.library_id)
        integration = await db.get(Integration, library.integration_id)
        integration.kind = kind
        identifier = integration.id
        db.add(
            AutomaticImportPolicy(
                destination_id=destination.id,
                approved_by=UUID(admin["id"]),
                enabled=True,
                configuration={},
            )
        )
        db.add(
            Operation(
                owner_id=UUID(admin["id"]),
                kind="library.sync",
                status="completed",
                integration_id=identifier,
                idempotency_key="historical-sync",
            )
        )
    response = await client.delete(f"/api/integrations/{identifier}")
    assert response.status_code == 204, response.text
    assert (await client.get("/api/integrations")).json() == []
    assert (await client.get("/api/library/libraries")).json() == []
    assert (await client.get("/api/organization/destinations")).json() == []
    async with database() as db:
        assert (await db.get(Integration, identifier)).deleted_at
        assert (
            await db.scalar(select(Operation).where(Operation.integration_id == identifier))
        ).status == "completed"
        assert not (await db.scalar(select(AutomaticImportPolicy))).enabled
    assert (await client.post(f"/api/integrations/{identifier}/test")).status_code == 404


async def test_folder_deletion_releases_root_key_and_keeps_connection(
    client,
    admin,
    database,
    selection_route,
):
    saved = (await client.get("/api/organization/destinations")).json()[0]
    url = f"/api/organization/destinations/{saved['id']}"
    assert (await client.delete(url, params={"expected_revision": "stale"})).status_code == 409
    response = await client.delete(url, params={"expected_revision": saved["revision"]})
    assert response.status_code == 204, response.text
    assert (await client.get("/api/organization/destinations")).json() == []
    assert len((await client.get("/api/integrations")).json()) == 1
    replacement = await client.put(
        "/api/organization/destinations/audio",
        json={
            "library_id": saved["library_id"],
            "medium": "audio",
            "backend_path": "/audiobooks",
        },
    )
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["id"] != saved["id"]


async def test_delete_blocks_selected_and_active_downloads_then_keeps_completed_history(
    client,
    admin,
    database,
    selection_route,
):
    response = await prepare(client, selection_route)
    assert response.status_code == 201, response.text
    selected = response.json()
    url = f"/api/downloaders/{selection_route['downloader_id']}"
    params = {"expected_generation": 1}
    assert (await client.delete(url, params=params)).status_code == 409
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        selection.state = "committed"
        operation = Operation(
            owner_id=UUID(admin["id"]), kind="download.submit", idempotency_key="active-download"
        )
        db.add(operation)
        await db.flush()
        attempt = DownloadAttempt(
            owner_id=UUID(admin["id"]),
            selection_id=selection.id,
            operation_id=operation.id,
            state="downloading",
            endpoint_key="a" * 64,
        )
        db.add(attempt)
        await db.flush()
        attempt_id = attempt.id
    blocked = await client.delete(url, params=params)
    assert blocked.status_code == 409 and "Unfinished downloads" in blocked.text
    async with database() as db, db.begin():
        (await db.get(DownloadAttempt, attempt_id)).state = "complete"
    response = await client.delete(url, params=params)
    assert response.status_code == 204, response.text
    async with database() as db:
        assert (await db.get(DownloadAttempt, attempt_id)).state == "complete"
        assert await db.get(AcquisitionSelection, UUID(selected["id"]))
    history = await client.get(f"/api/acquisition/selections/{selected['id']}")
    assert history.status_code == 200, history.text
    assert not history.json()["configuration_current"]


async def test_configuration_deletion_requires_admin(client, admin, database):
    saved = await downloader(client)
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "viewer"
    response = await client.delete(
        f"/api/downloaders/{saved['id']}", params={"expected_generation": 1}
    )
    assert response.status_code == 403


async def test_deleting_downloader_preserves_shared_mappings_and_files(
    client, admin, database, tmp_path
):
    from app.db.models import ImportStorageSettings

    first = await downloader(client)
    second = await downloader(client, base_url="http://other.test")
    own_path, shared_path = tmp_path / "own", tmp_path / "shared"
    own_path.mkdir()
    shared_path.mkdir()
    book = own_path / "book.epub"
    book.write_bytes(b"keep this book")
    mappings = [
        {"download_root": "/own", "source_key": "own", "source_path": str(own_path)},
        {"download_root": "/shared", "source_key": "shared", "source_path": str(shared_path)},
    ]
    async with database() as db, db.begin():
        db.add(
            ImportStorageSettings(
                id=1,
                destinations={},
                sources={
                    "own": str(own_path),
                    "shared": str(shared_path),
                },
            )
        )
        one = await db.get(Integration, UUID(first["id"]))
        two = await db.get(Integration, UUID(second["id"]))
        one.config = {**one.config, "mappings": mappings}
        two.config = {**two.config, "mappings": mappings[1:]}
    response = await client.delete(
        f"/api/downloaders/{first['id']}", params={"expected_generation": 1}
    )
    assert response.status_code == 204, response.text
    async with database() as db:
        assert (await db.get(ImportStorageSettings, 1)).sources == {"shared": str(shared_path)}
    assert book.read_bytes() == b"keep this book"


async def test_unfinished_import_blocks_folder_and_library_deletion_atomically(
    client,
    admin,
    database,
    selection_route,
    catalog,
):
    from uuid import uuid4

    from app.db.models import DownloadInspection, FrozenImportPlan, ImportEntry, ImportRun

    async with database() as db, db.begin():
        owner = UUID(admin["id"])
        destination = await db.get(ImportDestination, UUID(selection_route["destination_id"]))
        library = await db.get(Library, destination.library_id)
        integration_id = library.integration_id
        operation = Operation(
            owner_id=owner, kind="organization.inspect", idempotency_key="inspection"
        )
        db.add(operation)
        await db.flush()
        inspection = DownloadInspection(
            owner_id=owner,
            operation_id=operation.id,
            source_key="fixture",
            source_path="/downloads",
            relative_path="book",
            state="ready",
        )
        db.add(inspection)
        await db.flush()
        plan = FrozenImportPlan(
            owner_id=owner, inspection_id=inspection.id, revision="a" * 64, document={}
        )
        db.add(plan)
        await db.flush()
        run = ImportRun(owner_id=owner, plan_id=plan.id, command_key="import", request={})
        db.add(run)
        await db.flush()
        entry = ImportEntry(
            run_id=run.id,
            group_id=uuid4(),
            version_id=catalog["versions"][1],
            destination_id=destination.id,
            state="publishing",
            message="Importing",
        )
        db.add(entry)
        await db.flush()
        entry_id = entry.id
    saved = (await client.get("/api/organization/destinations")).json()[0]
    response = await client.delete(
        f"/api/organization/destinations/{saved['id']}",
        params={"expected_revision": saved["revision"]},
    )
    assert response.status_code == 409 and "Unfinished imports" in response.text
    response = await client.delete(f"/api/integrations/{integration_id}")
    assert response.status_code == 409 and "Unfinished imports" in response.text
    async with database() as db, db.begin():
        assert not (await db.get(Integration, integration_id)).deleted_at
        assert (await db.get(ImportDestination, UUID(saved["id"]))).enabled
        (await db.get(ImportEntry, entry_id)).state = "confirmed"
    assert (await client.delete(f"/api/integrations/{integration_id}")).status_code == 204
    async with database() as db:
        assert (await db.get(ImportEntry, entry_id)).state == "confirmed"
