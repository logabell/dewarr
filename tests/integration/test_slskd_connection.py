from uuid import UUID

import pytest

from app.config import get_settings
from app.db.models import ImportStorageSettings, Integration
from app.domain import downloaders, slskd_connection

pytestmark = pytest.mark.integration


@pytest.fixture
def slskd(monkeypatch, tmp_path):
    root = tmp_path.resolve() / "downloads"
    root.mkdir()
    state = {"root": str(root) + "/", "calls": 0}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def test(self):
            state["calls"] += 1
            return {"version": "0.26", "download_root": state["root"]}

    monkeypatch.setattr(slskd_connection, "SlskdClient", Client)
    monkeypatch.setattr(slskd_connection, "TEST_INTERVAL", 0)
    monkeypatch.setattr(get_settings(), "import_sources", {})
    monkeypatch.setattr(downloaders, "browse_roots", lambda settings: [tmp_path.resolve()])
    return state, root


async def connect(client):
    response = await client.put(
        "/api/sources/slskd/connection",
        json={
            "base_url": "http://slskd:5030",
            "api_key": "private-slskd-key-for-tests",
            "enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("existing", [False, True])
async def test_same_mounted_folder_is_discovered_and_shared_with_worker(
    client, admin, database, slskd, existing
):
    state, root = slskd
    if existing:
        async with database() as db, db.begin():
            db.add(ImportStorageSettings(id=1, destinations={}, sources={"saved-ui": str(root)}))
    saved = await connect(client)
    response = await client.post("/api/sources/slskd/connection/test")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected" and response.json()["mapped"]
    assert response.json()["download_root"] == str(root)
    listed = (await client.get("/api/downloaders")).json()
    assert len(listed) == 1 and listed[0]["kind"] == "slskd"
    assert listed[0]["mappings_current"]
    assert listed[0]["mappings"][0]["worker_path"] == str(root)
    if existing:
        assert listed[0]["mappings"][0]["source_key"] == "saved-ui"
    assert "private-slskd" not in str(listed) + response.text
    ready = (await client.get("/api/setup/readiness")).json()
    assert ready["downloaders"][0]["mappings_current"]
    assert any(source["key"] == "slskd" for source in ready["sources"])
    preview = await client.post(
        f"/api/downloaders/{saved['downloader_id']}/preview-path",
        json={
            "path": str(root / "Book"),
            "expected_generation": saved["downloader_generation"],
        },
    )
    assert preview.status_code == 200 and preview.json()["worker_path"] == str(root / "Book")
    options = (await client.get("/api/acquisition/selections/options")).json()
    assert options["downloaders"][0]["protocol"] == "soulseek"
    assert options["downloaders"][0]["ready"]
    assert state["calls"] == 1


async def test_remote_folder_uses_explicit_mapping_and_stale_mappings_are_not_ready(
    client, admin, database, slskd, monkeypatch
):
    state, root = slskd
    state["root"] = "/remote/downloads/"
    saved = await connect(client)
    tested = await client.post(f"/api/downloaders/{saved['downloader_id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["status"] == "connected" and not tested.json()["mappings_current"]
    url = f"/api/downloaders/{saved['downloader_id']}/mappings"
    body = {
        "expected_generation": saved["downloader_generation"],
        "mappings": [
            {"download_root": "/remote/downloads/", "worker_path": str(root)},
        ],
    }
    mapped = await client.put(url, json=body)
    assert mapped.status_code == 200, mapped.text
    assert mapped.json()["mappings_current"]
    assert (await client.put(url, json=body)).status_code == 409
    assert (await client.get("/api/sources/slskd/connection")).json()["mapped"]
    key = mapped.json()["mappings"][0]["source_key"]
    monkeypatch.setattr(get_settings(), "import_sources", {key: root.parent / "moved"})
    assert not (await client.get("/api/sources/slskd/connection")).json()["mapped"]
    async with database() as db:
        row = await db.get(Integration, UUID(saved["downloader_id"]))
        assert row.status == "connected"  # folder configuration is not connection health


async def test_mapping_rejects_library_overlap_and_preserves_shared_roots(
    client, admin, database, slskd, monkeypatch
):
    state, root = slskd
    saved = await connect(client)
    await client.post("/api/sources/slskd/connection/test")
    first = (await client.get("/api/downloaders")).json()[0]
    async with database() as db, db.begin():
        db.add(
            Integration(
                kind="qbittorrent",
                name="Other",
                base_url="http://qbit",
                encrypted_secrets="unused-by-this-test",
                config={
                    "save_path": str(root),
                    "category": "",
                    "mappings": [
                        {
                            "download_root": str(root),
                            "source_path": str(root),
                            "source_key": first["mappings"][0]["source_key"],
                        }
                    ],
                },
            )
        )
    url = f"/api/downloaders/{saved['downloader_id']}/mappings"
    cleared = await client.put(url, json={"expected_generation": 1, "mappings": []})
    assert cleared.status_code == 200, cleared.text
    async with database() as db:
        assert str(root) in (await db.get(ImportStorageSettings, 1)).sources.values()
    library = root.parent / "library"
    monkeypatch.setattr(get_settings(), "import_destinations", {"books": library})
    response = await client.put(
        url,
        json={
            "expected_generation": 2,
            "mappings": [{"download_root": str(root), "worker_path": str(library)}],
        },
    )
    assert response.status_code == 422


async def test_browsing_and_mapping_require_admin(client, slskd):
    assert (await client.get("/api/downloaders/folders")).status_code == 401
