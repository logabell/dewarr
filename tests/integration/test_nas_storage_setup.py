# ruff: noqa: F811
"""Issue #29: upgraded NAS setups must not need manual SQL or chmod."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select

from app.api import library_folders
from app.config import get_settings
from app.db.models import AuditEvent, ImportStorageSettings, Integration
from app.importing import destinations
from tests.integration.test_setup_probe import empty_route  # noqa: F401
from tests.integration.test_slskd_connection import connect, slskd  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("retained", [None, "deleted", "disabled", "environment", "inspection"])
async def test_library_save_reconciles_only_unreferenced_legacy_sources(
    client, admin, database, empty_route, monkeypatch, retained
):
    route = empty_route
    root = route["target"]
    incoming = root / "_incoming"
    stage = root / ".book-search-staging"
    stage.mkdir()
    stage.chmod(0o777)
    incoming.mkdir()
    marker = incoming / "keep.txt"
    marker.write_text("Existing files are never deleted by configuration cleanup")
    monkeypatch.setattr(library_folders, "Audiobookshelf", route["backend"].client)
    monkeypatch.setattr(destinations, "filesystem_mounts", lambda: [(root, "ext4", set())])
    monkeypatch.setattr(get_settings(), "import_staging_root", None)
    async with database() as db, db.begin():
        db.add(
            ImportStorageSettings(
                id=1,
                destinations={},
                sources={"incoming": str(incoming)},
                staging_root=str(stage),
            )
        )
        if retained in {"disabled", "deleted"}:
            db.add(
                Integration(
                    kind="qbittorrent",
                    name="Old client",
                    base_url="http://old.invalid",
                    encrypted_secrets="unused",
                    enabled=False,
                    deleted_at=datetime.now(UTC) if retained == "deleted" else None,
                    config={"mappings": [{"source_key": "incoming", "source_path": str(incoming)}]},
                )
            )
    if retained == "environment":
        monkeypatch.setattr(
            get_settings(),
            "import_sources",
            {**get_settings().import_sources, "incoming": incoming},
        )
    if retained == "inspection":
        response = await client.post(
            "/api/organization/inspections",
            headers={"Idempotency-Key": "retained-source"},
            json={
                "source_key": "incoming",
                "relative_path": "keep.txt",
                "completed_download": True,
            },
        )
        assert response.status_code == 202, response.text
    existing = (await client.get("/api/organization/destinations")).json()[0]
    body = {
        "library_id": existing["library_id"],
        "backend_path": "/books",
        "local_path": str(root),
        "destination_id": existing["id"],
        "expected_revision": existing["revision"],
    }
    response = await client.put("/api/organization/library-folders/ebook", json=body)
    protected = retained in {"disabled", "environment", "inspection"}
    assert response.status_code == (422 if protected else 200), response.text
    async with database() as db:
        sources = (await db.get(ImportStorageSettings, 1)).sources
        assert ("incoming" in sources) is protected
        events = list(
            await db.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "organization.download-sources.pruned"
                )
            )
        )
        assert len(events) == (0 if protected else 1)
    assert marker.read_text() == "Existing files are never deleted by configuration cleanup"
    if not protected:
        assert stage.stat().st_mode & 0o777 == 0o700
        # Repeated saves and the second medium use the same root and legacy journals.
        shared = await client.put(
            "/api/organization/library-folders/audio",
            json={
                "library_id": existing["library_id"],
                "backend_path": "/books",
                "local_path": str(root),
            },
        )
        assert shared.status_code == 200, shared.text
        assert shared.json()["shared_root"]
    else:
        assert "incoming" in response.json()["detail"]
        assert stage.stat().st_mode & 0o777 == 0o777


@pytest.mark.parametrize("shared", [False, True])
async def test_endpoint_change_retires_mapping_without_hidden_sources(
    client, admin, database, monkeypatch, tmp_path, shared
):
    monkeypatch.setattr(get_settings(), "import_sources", {})
    root = str(tmp_path / "downloads")
    response = await client.post(
        "/api/downloaders",
        json={
            "base_url": "http://old.invalid",
            "save_path": "/remote/downloads",
            "mappings": [{"download_root": "/remote/downloads", "worker_path": root}],
        },
    )
    assert response.status_code == 201, response.text
    connection = response.json()
    if shared:
        response = await client.post(
            "/api/downloaders",
            json={
                "base_url": "http://shared.invalid",
                "save_path": "/remote/downloads",
                "mappings": [{"download_root": "/remote/downloads", "worker_path": root}],
                "enabled": False,
            },
        )
        assert response.status_code == 201, response.text
    response = await client.put(
        f"/api/downloaders/{connection['id']}",
        json={
            "base_url": "http://new.invalid",
            "expected_generation": connection["generation"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["mappings"] == []
    async with database() as db:
        assert (await db.get(ImportStorageSettings, 1)).sources == (
            {"downloads": root} if shared else {}
        )
        assert (await db.get(Integration, UUID(connection["id"]))).config["mappings"] == []


async def test_soulseek_endpoint_change_retires_its_previous_source(client, admin, database, slskd):
    saved = await connect(client)
    response = await client.post("/api/sources/slskd/connection/test")
    assert response.status_code == 200 and response.json()["mapped"]
    response = await client.put(
        "/api/sources/slskd/connection",
        json={
            "base_url": "http://replacement:5030",
            "api_key": "replacement-test-key",
            "enabled": True,
            "expected_generation": saved["generation"],
        },
    )
    assert response.status_code == 200, response.text
    async with database() as db:
        assert (await db.get(ImportStorageSettings, 1)).sources == {}


async def test_removing_a_mapping_keeps_inspected_sources_and_ignores_deleted_clients(
    client, admin, database, monkeypatch, tmp_path
):
    monkeypatch.setattr(get_settings(), "import_sources", {})
    root = tmp_path.resolve() / "downloads"
    root.mkdir()
    (root / "keep.txt").write_text("retained inspection source")
    body = {
        "base_url": "http://first.invalid",
        "save_path": "/remote/downloads",
        "mappings": [{"download_root": "/remote/downloads", "worker_path": str(root)}],
    }
    first = await client.post("/api/downloaders", json=body)
    assert first.status_code == 201, first.text
    second = await client.post(
        "/api/downloaders", json={**body, "base_url": "http://deleted.invalid"}
    )
    assert second.status_code == 201, second.text
    async with database() as db, db.begin():
        (await db.get(Integration, UUID(second.json()["id"]))).deleted_at = datetime.now(UTC)
    # A tombstone does not pin a mapping after its last live client removes it.
    endpoint = f"/api/downloaders/{first.json()['id']}/mappings"
    cleared = await client.put(endpoint, json={"expected_generation": 1, "mappings": []})
    assert cleared.status_code == 200, cleared.text
    async with database() as db:
        assert (await db.get(ImportStorageSettings, 1)).sources == {}
    restored = await client.put(
        endpoint, json={"expected_generation": 2, "mappings": body["mappings"]}
    )
    assert restored.status_code == 200, restored.text
    inspected = await client.post(
        "/api/organization/inspections",
        headers={"Idempotency-Key": "history-protection"},
        json={"source_key": "downloads", "relative_path": "keep.txt", "completed_download": True},
    )
    assert inspected.status_code == 202, inspected.text
    cleared = await client.put(endpoint, json={"expected_generation": 3, "mappings": []})
    assert cleared.status_code == 200, cleared.text
    async with database() as db:
        assert (await db.get(ImportStorageSettings, 1)).sources == {"downloads": str(root)}
