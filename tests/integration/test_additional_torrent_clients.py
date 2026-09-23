# ruff: noqa: F401, F811
"""New clients share the actual dispatch marker, path mapping and fulfillment lifecycle."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    AcquisitionSelection,
    AcquisitionTarget,
    DownloadAttempt,
    DownloadInspection,
    Integration,
)
from app.domain import download_attempts as downloads
from app.domain.acquisition_selection import configuration_current
from tests.integration.test_acquisition import catalog
from tests.integration.test_acquisition_selections import prepare, selection_route
from tests.integration.test_download_attempts import Client, row, start
from tests.integration.test_download_fulfillment import asset, reconcile

pytestmark = pytest.mark.integration


@pytest.fixture(params=["transmission", "deluge"])
async def torrent_client(request, database, client, admin, selection_route, monkeypatch):
    kind = request.param
    async with database() as db, db.begin():
        connection = await db.get(Integration, UUID(selection_route["downloader_id"]))
        connection.kind = kind
        connection.capabilities = {"operations": ["categories", "submit", "find"]}
    response = await prepare(client, selection_route)
    assert response.status_code == 201, response.text
    selected = response.json()
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        assert await configuration_current(db, selection)
        fake = Client(database, selection.frozen["descriptor"])
        if kind == "deluge":
            assert "/dewarr-" in selection.frozen["downloader"]["save_path"]
            assert selection.frozen["mapping"]["relative_path"].startswith("dewarr-")
    monkeypatch.setattr(
        downloads,
        "TransmissionClient" if kind == "transmission" else "DelugeClient",
        lambda *args: fake,
    )
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    return kind, selected, fake


async def test_crash_during_add_recovers_by_observation(database, client, torrent_client):
    kind, selected, fake = torrent_client

    class Crash(BaseException):
        pass

    response = await start(client, selected)
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    fake.fail = Crash()
    with pytest.raises(Crash):
        await downloads.run(identifier)
    if kind == "deluge":
        fake.states[0].tags = set()
    async with database() as db, db.begin():
        attempt = await db.get(DownloadAttempt, identifier)
        assert attempt.external_may_exist
        attempt.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await downloads.run(identifier)
    assert (await row(database, str(identifier))).state == "downloading"
    assert fake.calls.count("submit") == 1


async def test_mapped_completion_waits_for_library_confirmation(
    database, client, catalog, torrent_client
):
    kind, selected, fake = torrent_client
    fake.complete = True
    response = await start(client, selected)
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    await downloads.run(identifier)
    async with database() as db:
        attempt = await db.get(DownloadAttempt, identifier)
        assert attempt.state == "complete"
        inspection = await db.get(DownloadInspection, attempt.inspection_id)
        assert inspection.source_key == "fixture"
        if kind == "deluge":
            assert inspection.relative_path.startswith("dewarr-")
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        target_id = selection.target_id
        assert (await db.get(AcquisitionTarget, target_id)).state == "wanted"
    await asset(database, catalog)
    await reconcile(database, catalog["work"])
    async with database() as db:
        assert (await db.get(AcquisitionTarget, target_id)).state == "satisfied"
        assert (await db.get(AcquisitionSelection, UUID(selected["id"]))).state == "fulfilled"
    await downloads.run(identifier)
    assert fake.calls.count("submit") == 1


async def test_preexisting_transfer_is_never_adopted(database, client, torrent_client):
    _, selected, fake = torrent_client
    from app.adapters.qbittorrent import QbitState

    fake.states = [
        QbitState(
            external_id=fake.descriptor["infohash_v1"],
            infohash_v1=fake.descriptor["infohash_v1"],
            state="downloading",
            completed=False,
            save_path="/downloads",
            category="book-search",
            auto_managed=False,
            progress=0.2,
            total_bytes=24,
            all_files_selected=True,
        )
    ]
    response = await start(client, selected)
    identifier = UUID(response.json()["id"])
    await downloads.run(identifier)
    assert (await row(database, str(identifier))).state == "held"
    assert "submit" not in fake.calls


@pytest.mark.parametrize("kind", ["transmission", "deluge"])
async def test_connect_test_and_choose_route(client, admin, monkeypatch, tmp_path, kind):
    import json

    import httpx

    from app.adapters.deluge import DelugeClient
    from app.adapters.transmission import TransmissionClient
    from app.domain import downloaders

    monkeypatch.setattr(get_settings(), "import_sources", {"new-client": tmp_path})
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload["method"])
        if kind == "transmission":
            assert request.url.path == "/transmission/rpc"
            return httpx.Response(
                200,
                json={"result": "success", "arguments": {"rpc-version": 17, "version": "4.0.6"}},
            )
        assert request.url.path == "/json"
        values = {
            "auth.login": True,
            "web.connected": True,
            "daemon.info": "2.2",
            "core.get_enabled_plugins": ["Label"],
        }
        return httpx.Response(
            200, json={"id": payload["id"], "result": values[payload["method"]], "error": None}
        )

    cls = TransmissionClient if kind == "transmission" else DelugeClient
    monkeypatch.setattr(
        downloaders,
        "TransmissionClient" if kind == "transmission" else "DelugeClient",
        lambda *args: cls(*args, transport=httpx.MockTransport(handler)),
    )
    response = await client.post(
        "/api/downloaders",
        json={
            "kind": kind,
            "name": kind,
            "base_url": f"http://{kind}.test",
            "password": "private-secret",
            "save_path": "/downloads",
            "mappings": [{"download_root": "/downloads", "source_key": "new-client"}],
        },
    )
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    tested = await client.post(f"/api/downloaders/{identifier}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["status"] == "connected"
    assert tested.json()["capabilities"]["in_client_rename"] is False
    assert tested.json()["capabilities"]["attempt_tagging"] == (kind == "transmission")
    assert "private-secret" not in tested.text
    options = (await client.get("/api/acquisition/selections/options")).json()
    choice = next(c for c in options["downloaders"] if c["id"] == identifier)
    assert choice["protocol"] == "torrent" and choice["ready"]
    assert not any("add" in method for method in calls)
