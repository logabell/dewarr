import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.adapters.qbittorrent import QbitClient
from app.config import get_settings
from app.db.models import AuditEvent, ImportStorageSettings, Integration, User
from app.domain import downloaders
from app.security import decrypt_secrets


@pytest.fixture
def downloader_http(monkeypatch, tmp_path):
    roots = {"downloads": tmp_path / "worker", "other": tmp_path / "other"}
    monkeypatch.setattr(get_settings(), "import_sources", roots)
    monkeypatch.setattr(downloaders, "TEST_INTERVAL", 0)
    state = {"calls": [], "entered": asyncio.Event(), "wait": None, "status": 204}

    async def handler(request):
        state["calls"].append(request)
        if request.url.path.endswith("auth/login"):
            state["entered"].set()
            if state["wait"]:
                await state["wait"].wait()
            return httpx.Response(state["status"], headers={"set-cookie": "SID=fixture; Path=/"})
        assert request.headers["cookie"] == "SID=fixture"
        if request.url.path.endswith("app/version"):
            return httpx.Response(200, text="v5.2.3")
        if request.url.path.endswith("app/webapiVersion"):
            return httpx.Response(200, text="2.15.1")
        raise AssertionError("Connection tests must not query or mutate transfers")

    monkeypatch.setattr(
        downloaders,
        "QbitClient",
        lambda *args, **kwargs: QbitClient(*args, **kwargs, transport=httpx.MockTransport(handler)),
    )
    state["roots"] = roots
    return state


def config(**changes):
    return {
        "name": "Books qBit",
        "base_url": "http://qbit.test",
        "username": "private-qbit-user",
        "password": "private-qbit-password",
        "save_path": "/data/downloads/books",
        "mappings": [{"download_root": "/data/downloads", "source_key": "downloads"}],
        **changes,
    }


async def create(client, **changes):
    response = await client.post("/api/downloaders", json=config(**changes))
    assert response.status_code == 201, response.text
    return response.json()


async def test_saved_connection_encryption_redaction_scoping_and_health(
    client, admin, database, downloader_http
):
    record = await create(client)
    assert record["has_credentials"] and not record["dispatch_available"]
    assert record["mappings_current"] and record["generation"] == 1
    identifier = record["id"]
    tested = await client.post(f"/api/downloaders/{identifier}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["version"] == "v5.2.3" and tested.json()["status"] == "connected"
    listed = await client.get("/api/downloaders")
    assert "private-qbit" not in tested.text + listed.text + str(record)
    assert (await client.get("/api/integrations")).json() == []
    assert (await client.post(f"/api/integrations/{identifier}/test")).status_code == 404
    assert (
        await client.post(
            f"/api/integrations/{identifier}/sync",
            headers={"idempotency-key": "downloader-boundary-test"},
        )
    ).status_code == 404
    async with database() as db:
        row = await db.get(Integration, UUID(identifier))
        assert "private-qbit" not in row.encrypted_secrets
        assert decrypt_secrets(row.encrypted_secrets)["password"] == "private-qbit-password"
        assert row.lease_token is None and row.last_success_at
        assert "submit" in row.capabilities["operations"]
        assert "private-qbit" not in str(
            [event.detail for event in await db.scalars(select(AuditEvent))]
        )
    assert len(downloader_http["calls"]) == 3


async def test_path_mapping_is_confined_and_not_filesystem_verification(
    client, admin, downloader_http
):
    record = await create(client)
    url = f"/api/downloaders/{record['id']}/preview-path"
    for path, relative in [
        ("/data/downloads", ""),
        ("/data/downloads/books/One/book.m4b", "books/One/book.m4b"),
    ]:
        preview = await client.post(url, json={"path": path, "expected_generation": 1})
        assert preview.status_code == 200, preview.text
        assert preview.json()["relative_path"] == relative
        assert preview.json()["worker_path"] == str(
            downloader_http["roots"]["downloads"] / relative
        )
        assert not preview.json()["filesystem_verified"]
    for path in [
        "/data/downloads-other/book",
        "/data/downloads/../secret",
        "/etc/passwd",
        "relative",
        "/data\\downloads",
    ]:
        assert (
            await client.post(url, json={"path": path, "expected_generation": 1})
        ).status_code == 422
    assert not downloader_http["calls"]


async def test_changed_worker_root_invalidates_mapping_until_saved(
    client, admin, downloader_http, monkeypatch, tmp_path
):
    record = await create(client)
    monkeypatch.setattr(get_settings(), "import_sources", {"downloads": tmp_path / "moved"})
    assert not (await client.get("/api/downloaders")).json()[0]["mappings_current"]
    url = f"/api/downloaders/{record['id']}"
    assert (
        await client.post(
            url + "/preview-path", json={"path": "/data/downloads/book", "expected_generation": 1}
        )
    ).status_code == 409
    updated = await client.put(
        url, json=config(username=None, password=None, expected_generation=1)
    )
    assert updated.status_code == 200 and updated.json()["mappings_current"]
    assert (
        await client.post(
            url + "/preview-path", json={"path": "/data/downloads/book", "expected_generation": 1}
        )
    ).status_code == 409
    assert (
        await client.post(
            url + "/preview-path", json={"path": "/data/downloads/book", "expected_generation": 2}
        )
    ).status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"mappings": []},
        {"mappings": [{"download_root": "/data", "source_key": "missing"}]},
        {
            "mappings": [
                {"download_root": "/data", "source_key": "downloads"},
                {"download_root": "/data/downloads", "source_key": "other"},
            ]
        },
        {"save_path": "/elsewhere/books"},
        {"save_path": "/data/../secret"},
        {"base_url": "http://private-qbit-password@qbit.test"},
        {"category": "one,two"},
    ],
)
async def test_invalid_settings_never_disclose_input_or_contact_server(
    client, admin, downloader_http, changes
):
    response = await client.post("/api/downloaders", json=config(**changes))
    assert response.status_code == 422, response.text
    assert "private-qbit" not in response.text
    assert not downloader_http["calls"]


async def test_stale_edits_endpoint_replacement_and_duplicate_creation(
    client, admin, database, downloader_http
):
    record = await create(client)
    url = f"/api/downloaders/{record['id']}"
    assert (await client.post("/api/downloaders", json=config())).status_code == 409
    assert (await client.put(url, json=config())).status_code == 409
    updated = await client.put(
        url,
        json=config(
            base_url="http://new.test",
            username="new-user",
            password="new-password",
            expected_generation=1,
        ),
    )
    assert updated.status_code == 200 and updated.json()["generation"] == 2
    async with database() as db:
        row = await db.get(Integration, UUID(record["id"]))
        assert decrypt_secrets(row.encrypted_secrets) == {
            "username": "new-user",
            "password": "new-password",
        }


async def test_connection_edits_fence_inflight_health_results(
    client, admin, database, downloader_http
):
    record = await create(client)
    url = f"/api/downloaders/{record['id']}"
    downloader_http["wait"] = asyncio.Event()
    task = asyncio.create_task(client.post(url + "/test"))
    await asyncio.wait_for(downloader_http["entered"].wait(), 5)
    assert (await client.post(url + "/test")).status_code == 429
    edited = await client.put(
        url, json=config(password="replacement-password", expected_generation=1)
    )
    assert edited.status_code == 200
    downloader_http["wait"].set()
    assert (await task).status_code == 409
    async with database() as db:
        row = await db.get(Integration, UUID(record["id"]))
        assert row.status == "untested" and not row.capabilities and row.lease_token is None
        assert decrypt_secrets(row.encrypted_secrets)["password"] == "replacement-password"


async def test_failed_test_cooldown_survives_edits_and_disabled_connection(
    client, admin, database, downloader_http
):
    record = await create(client)
    url = f"/api/downloaders/{record['id']}"
    downloader_http["status"] = 401
    assert (await client.post(url + "/test")).status_code == 409
    assert (await client.get("/api/downloaders")).json()[0]["status"] == "authentication"
    await client.put(url, json=config(expected_generation=1))
    cooldown = await client.post(url + "/test")
    assert cooldown.status_code == 429 and int(cooldown.headers["retry-after"]) > 0
    await client.put(url, json=config(enabled=False, expected_generation=2))
    assert (await client.post(url + "/test")).status_code == 409
    assert len(downloader_http["calls"]) == 1


async def test_revoked_admin_cannot_receive_inflight_test_results(
    client, admin, database, downloader_http
):
    record = await create(client)
    downloader_http["wait"] = asyncio.Event()
    task = asyncio.create_task(client.post(f"/api/downloaders/{record['id']}/test"))
    await asyncio.wait_for(downloader_http["entered"].wait(), 5)
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "member"
    downloader_http["wait"].set()
    assert (await task).status_code == 403
    assert (await client.get("/api/downloaders")).status_code == 403
    assert (await client.post("/api/downloaders", json=config())).status_code == 403


async def test_cancellation_leaves_expiring_diagnostic_lease(
    client, admin, database, downloader_http
):
    record = await create(client)
    url = f"/api/downloaders/{record['id']}/test"
    downloader_http["wait"] = asyncio.Event()
    task = asyncio.create_task(client.post(url))
    await asyncio.wait_for(downloader_http["entered"].wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await client.post(url)).status_code == 429
    async with database() as db, db.begin():
        row = await db.get(Integration, UUID(record["id"]))
        assert row.lease_token is not None
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    downloader_http["wait"] = None
    assert (await client.post(url)).status_code == 200


async def test_csrf_and_library_connection_type_boundaries(
    client, admin, database, downloader_http
):
    record = await create(client)
    assert (
        await client.post(
            f"/api/downloaders/{record['id']}/test", headers={"Origin": "http://other.test"}
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/api/downloaders/{record['id']}/test", headers={"X-CSRF-Token": "wrong"}
        )
    ).status_code == 403
    async with database() as db, db.begin():
        (await db.get(Integration, UUID(record["id"]))).kind = "audiobookshelf"
    assert (await client.get("/api/downloaders")).json() == []
    assert (await client.post(f"/api/downloaders/{record['id']}/test")).status_code == 404
    assert not downloader_http["calls"]


async def test_multiple_disjoint_mappings_preserve_the_correct_worker_root(
    client, admin, downloader_http
):
    record = await create(
        client,
        mappings=[
            {"download_root": "/data/downloads", "source_key": "downloads"},
            {"download_root": "/archive", "source_key": "other"},
        ],
    )
    response = await client.post(
        f"/api/downloaders/{record['id']}/preview-path",
        json={"path": "/archive/Series/Book", "expected_generation": 1},
    )
    assert response.status_code == 200
    assert response.json()["source_key"] == "other"
    assert response.json()["worker_path"] == str(downloader_http["roots"]["other"] / "Series/Book")


async def test_concurrent_creates_and_edits_do_not_overwrite_settings(
    client, admin, downloader_http
):
    responses = await asyncio.gather(
        client.post("/api/downloaders", json=config()),
        client.post("/api/downloaders", json=config()),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    record = next(response.json() for response in responses if response.status_code == 201)
    responses = await asyncio.gather(
        client.put(
            f"/api/downloaders/{record['id']}", json=config(name="First", expected_generation=1)
        ),
        client.put(
            f"/api/downloaders/{record['id']}", json=config(name="Second", expected_generation=1)
        ),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert (await client.get("/api/downloaders")).json()[0]["generation"] == 2


async def test_overall_diagnostic_timeout_releases_lease_and_records_failure(
    client, admin, database, downloader_http, monkeypatch
):
    record = await create(client)
    downloader_http["wait"] = asyncio.Event()
    monkeypatch.setattr(downloaders, "TEST_TIMEOUT", 0.01)
    response = await client.post(f"/api/downloaders/{record['id']}/test")
    assert response.status_code == 503
    async with database() as db:
        row = await db.get(Integration, UUID(record["id"]))
        assert row.status == "timeout" and row.lease_token is None
        assert not row.capabilities and row.next_sync_at > datetime.now(UTC)


async def test_simple_connection_needs_no_credentials_or_worker_roots(
    client, admin, database, monkeypatch
):
    monkeypatch.setattr(get_settings(), "import_sources", {})
    response = await client.post(
        "/api/downloaders", json={"base_url": "10.0.0.2:8080", "category": "books"}
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["base_url"] == "http://10.0.0.2:8080"
    assert not record["has_credentials"]
    assert record["mappings"] == []
    async with database() as db:
        row = await db.get(Integration, UUID(record["id"]))
        assert row.config["client_managed"]
        assert decrypt_secrets(row.encrypted_secrets) == {"username": "", "password": ""}


async def test_changing_endpoint_drops_saved_credentials(client, admin, database, downloader_http):
    record = await create(client)
    response = await client.put(
        f"/api/downloaders/{record['id']}",
        json={
            "base_url": "http://other.test",
            "expected_generation": 1,
        },
    )
    assert response.status_code == 200, response.text
    assert not response.json()["has_credentials"]


async def test_simple_connection_test_reads_folder_without_requiring_mounts(
    client, admin, monkeypatch
):
    monkeypatch.setattr(get_settings(), "import_sources", {})
    calls = []

    async def handler(request):
        calls.append(request)
        responses = {
            "app/version": httpx.Response(200, text="v5.2.3"),
            "app/webapiVersion": httpx.Response(200, text="2.15.1"),
            "app/preferences": httpx.Response(
                200,
                json={
                    "save_path": "/remote/downloads",
                    "auto_tmm_enabled": True,
                },
            ),
            "torrents/categories": httpx.Response(
                200,
                json={
                    "books": {"savePath": "/remote/books"},
                },
            ),
        }
        return responses[request.url.path.removeprefix("/api/v2/")]

    monkeypatch.setattr(
        downloaders,
        "QbitClient",
        lambda *args: QbitClient(
            *args,
            transport=httpx.MockTransport(handler),
        ),
    )
    response = await client.post(
        "/api/downloaders", json={"base_url": "qbit.test:8080", "category": "books"}
    )
    record = response.json()
    tested = await client.post(f"/api/downloaders/{record['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["status"] == "connected"
    assert tested.json()["save_path"] == "/remote/books"
    assert not tested.json()["mappings_current"]
    assert all(request.method == "GET" for request in calls)


async def test_remote_path_map_translates_client_paths_to_a_worker_folder(
    client, admin, database, monkeypatch, tmp_path
):
    monkeypatch.setattr(get_settings(), "import_sources", {})
    library, stage, worker = (tmp_path / name for name in ("library", "stage", "downloads"))
    monkeypatch.setattr(get_settings(), "import_destinations", {"ebooks": library})
    monkeypatch.setattr(get_settings(), "import_staging_root", stage)
    created = await client.post(
        "/api/downloaders",
        json={"base_url": "http://qbit.test", "username": "kept-user", "password": "kept-secret"},
    )
    assert created.status_code == 201, created.text
    record = created.json()
    early = await client.put(
        f"/api/downloaders/{record['id']}",
        json={
            "base_url": record["base_url"],
            "expected_generation": record["generation"],
            "mappings": [{"download_root": "/remote/downloads", "worker_path": str(worker)}],
        },
    )
    assert early.status_code == 422
    async with database() as db, db.begin():
        row = await db.get(Integration, UUID(record["id"]))
        row.config = {**row.config, "save_path": "/remote/downloads/books"}
        generation = row.credential_generation
    overlap = await client.put(
        f"/api/downloaders/{record['id']}",
        json={
            "base_url": record["base_url"],
            "expected_generation": generation,
            "mappings": [{"download_root": "/remote/downloads", "worker_path": str(library)}],
        },
    )
    assert overlap.status_code == 422
    escaped = await client.put(
        f"/api/downloaders/{record['id']}",
        json={
            "base_url": record["base_url"],
            "expected_generation": generation,
            "mappings": [{"download_root": "/remote/downloads", "worker_path": "/data/../secret"}],
        },
    )
    assert escaped.status_code == 422
    mapped = await client.put(
        f"/api/downloaders/{record['id']}",
        json={
            "base_url": record["base_url"],
            "expected_generation": generation,
            "mappings": [{"download_root": "/remote/downloads", "worker_path": str(worker)}],
        },
    )
    assert mapped.status_code == 200, mapped.text
    body = mapped.json()
    assert body["mappings_current"] and body["status"] == "untested"
    assert body["mappings"] == [
        {
            "download_root": "/remote/downloads",
            "source_key": "downloads",
            "worker_path": str(worker),
        }
    ]
    preview = await client.post(
        f"/api/downloaders/{record['id']}/preview-path",
        json={
            "path": "/remote/downloads/books/Title/book.m4b",
            "expected_generation": body["generation"],
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["relative_path"] == "books/Title/book.m4b"
    assert preview.json()["worker_path"] == str(worker / "books/Title/book.m4b")
    assert not preview.json()["filesystem_verified"]
    async with database() as db:
        stored = await db.get(ImportStorageSettings, 1)
        row = await db.get(Integration, UUID(record["id"]))
        assert stored.sources == {"downloads": str(worker)}
        assert row.config["client_managed"]
        assert decrypt_secrets(row.encrypted_secrets)["password"] == "kept-secret"
    moved = tmp_path / "elsewhere"
    replaced = await client.put(
        f"/api/downloaders/{record['id']}",
        json={
            "base_url": record["base_url"],
            "expected_generation": body["generation"],
            "mappings": [{"download_root": "/remote/downloads", "worker_path": str(moved)}],
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["mappings"][0]["worker_path"] == str(moved)
    async with database() as db:
        assert (await db.get(ImportStorageSettings, 1)).sources == {"elsewhere": str(moved)}


@pytest.mark.parametrize("shared, protected", [(True, False), (False, False), (True, True)])
async def test_client_managed_shared_volume_is_automatically_bound(
    client, admin, database, monkeypatch, tmp_path, shared, protected
):
    from app.domain import download_folders

    volume = tmp_path / "mounted"
    folder = volume / "torrents" / "ebooks"
    folder.mkdir(parents=True)
    reported = str(folder) if shared else "/remote/torrents/ebooks"
    expected_mapping = shared and not protected
    monkeypatch.setattr(
        get_settings(), "import_destinations", {"ebooks": folder} if protected else {}
    )
    monkeypatch.setattr(get_settings(), "import_sources", {})
    monkeypatch.setattr(download_folders, "volume_roots", lambda: {volume})

    async def handler(request):
        assert request.method == "GET"
        return {
            "app/version": httpx.Response(200, text="v5.2.3"),
            "app/webapiVersion": httpx.Response(200, text="2.15.1"),
            "app/preferences": httpx.Response(200, json={"save_path": reported}),
            "torrents/categories": httpx.Response(200, json={}),
        }[request.url.path.removeprefix("/api/v2/")]

    monkeypatch.setattr(
        downloaders,
        "QbitClient",
        lambda *args: QbitClient(*args, transport=httpx.MockTransport(handler)),
    )
    record = (await client.post("/api/downloaders", json={"base_url": "http://qbit.test"})).json()
    response = await client.post(f"/api/downloaders/{record['id']}/test")
    assert response.status_code == 200, response.text
    tested = response.json()
    assert tested["status"] == "connected"
    assert tested["mappings_current"] is expected_mapping
    assert tested["save_path"] == reported
    async with database() as db:
        storage = await db.get(ImportStorageSettings, 1)
        if expected_mapping:
            assert tested["mappings"][0]["worker_path"] == reported
            assert storage.sources == {"ebooks": reported}
        else:
            assert not tested["mappings"]
            assert storage is None


async def test_download_folder_browser_is_admin_only(client):
    assert (await client.get("/api/downloaders/folders")).status_code == 401


async def test_download_folder_browser_lists_mounted_directories(
    client, admin, monkeypatch, tmp_path
):
    from app.domain import download_folders

    (tmp_path / "ebooks").mkdir()
    monkeypatch.setattr(download_folders, "volume_roots", lambda: {tmp_path})
    monkeypatch.setattr(get_settings(), "import_sources", {})
    assert (await client.get("/api/downloaders/folders")).json()["directories"] == [str(tmp_path)]
    listed = await client.get("/api/downloaders/folders", params={"path": str(tmp_path)})
    assert listed.json()["directories"] == [str(tmp_path / "ebooks")]
    assert (
        await client.get("/api/downloaders/folders", params={"path": "/etc"})
    ).status_code == 422


async def test_mapping_can_be_removed_and_endpoint_changes_clear_detected_folder(
    client, admin, downloader_http
):
    record = await create(client)
    cleared = await client.put(
        f"/api/downloaders/{record['id']}",
        json={"base_url": record["base_url"], "expected_generation": 1, "mappings": []},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["mappings"] == []
    changed = await client.put(
        f"/api/downloaders/{record['id']}",
        json={"base_url": "http://different.test", "expected_generation": 2},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["save_path"] == ""
