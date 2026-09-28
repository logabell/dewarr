# ruff: noqa: F811
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest

from app.adapters.contracts import SubmissionReceipt
from app.adapters.prowlarr import ProwlarrRelease
from app.adapters.sabnzbd import SabClient, SabState
from app.config import get_settings
from app.db.models import DownloadAttempt, DownloadInspection, Integration, Operation, SourceResult
from app.domain import automatic_selection
from app.domain import download_attempts as downloads
from app.domain import downloaders as downloader_settings
from app.security import decrypt_secrets, encrypt_secrets
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401
from tests.integration.test_automatic_dispatch import authorized  # noqa: F401
from tests.integration.test_automatic_selection import source  # noqa: F401
from tests.integration.test_download_attempts import start
from tests.integration.test_prowlarr_sources import (
    configure,
    prowlarr_http,  # noqa: F401
    resolve,
    search,
)
from tests.integration.test_quick_add import defaults
from tests.nzb_fixture import nzb_bytes
from tests.prowlarr_fixture import release

pytestmark = pytest.mark.integration


def sab_body(**changes):
    return {
        "kind": "sabnzbd",
        "name": "SABnzbd",
        "base_url": "http://sab.test:8080",
        "api_key": "private-sab-key",
        "category": "books",
        **changes,
    }


@pytest.mark.parametrize("version", ["3.7.2", "4.5.1", "5.1.1", "5.1.3"])
async def test_sabnzbd_connection_keeps_the_api_key_private(
    client, admin, database, monkeypatch, version
):
    monkeypatch.setattr(get_settings(), "import_sources", {})
    calls = []

    async def handler(request):
        calls.append(request)
        assert "private-sab-key" not in str(request.url)
        assert "x-api-key" not in request.headers
        params = parse_qs(request.content.decode())
        assert params["apikey"] == ["private-sab-key"]
        mode = params["mode"][0]
        if mode == "version":
            return httpx.Response(200, json={"version": version})
        if mode == "get_config" and params["section"] == ["misc"]:
            return httpx.Response(
                200, json={"config": {"misc": {"complete_dir": "/downloads/complete"}}}
            )
        if mode == "get_config":
            return httpx.Response(
                200, json={"config": {"categories": [{"name": "books", "dir": "/downloads/books"}]}}
            )
        raise AssertionError(mode)

    monkeypatch.setattr(
        downloader_settings,
        "SabClient",
        lambda *args, **kwargs: SabClient(*args, transport=httpx.MockTransport(handler), **kwargs),
    )
    created = await client.post("/api/downloaders", json=sab_body())
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "sabnzbd"
    assert created.json()["has_credentials"]
    assert "private-sab-key" not in created.text
    tested = await client.post(f"/api/downloaders/{created.json()['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["status"] == "connected"
    assert tested.json()["version"] == version
    assert tested.json()["save_path"] == "/downloads/books"
    assert "private-sab-key" not in tested.text
    async with database() as db:
        row = await db.get(Integration, UUID(created.json()["id"]))
        assert decrypt_secrets(row.encrypted_secrets)["api_key"] == "private-sab-key"
        assert "private-sab-key" not in row.encrypted_secrets
    assert all(request.method == "POST" for request in calls)
    qbit = await client.post(
        "/api/downloaders",
        json={"base_url": "http://sab.test:8080", "category": "books"},
    )
    assert qbit.status_code == 201, qbit.text
    listed = await client.get("/api/downloaders")
    assert {item["kind"] for item in listed.json()} == {"qbittorrent", "sabnzbd"}
    assert "private-sab-key" not in listed.text


async def test_prowlarr_usenet_nzb_can_be_inspected(client, admin, prowlarr_http):
    await configure(client)
    prowlarr_http["releases"] = [release(protocol="usenet", categories=[{"id": 3030}])]
    prowlarr_http["bytes"] = nzb_bytes()
    found = await search(client)
    assert found.status_code == 200, found.text
    item = found.json()["items"][0]
    assert item["release"]["protocol"] == "nzb"
    assert item["release"]["acquisition_supported"]
    inspected = await resolve(client, item["id"])
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["descriptor"]["protocol"] == "nzb"
    assert inspected.json()["descriptor"]["files"][0]["path"].endswith(".m4b")
    assert "part@example" not in inspected.text


class Grab:
    def __init__(self):
        self.calls = []
        self.tag = None
        self.category = None
        self.complete = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def capabilities(self):
        return None

    async def find(self, **kwargs):
        self.calls.append("find")
        if not self.tag:
            return []
        if self.complete:
            return [
                SabState(
                    external_id="SABnzbd_nzo_test",
                    state="Completed",
                    completed=True,
                    save_path="/downloads/Finished Book",
                    category=self.category,
                    names={self.tag},
                    reported_complete=True,
                )
            ]
        return [
            SabState(
                external_id="SABnzbd_nzo_test",
                state="Downloading",
                completed=False,
                save_path="/pending",
                category=self.category,
                names={self.tag},
            )
        ]

    async def submit(self, content, *, attempt_tag, save_path, category):
        self.calls.append("submit")
        assert b"<nzb" in content
        assert save_path == "/downloads"
        self.tag, self.category = attempt_tag, category
        return SubmissionReceipt(external_ids=["SABnzbd_nzo_test"])


async def test_clicked_usenet_release_with_parenthesized_format_reaches_downloader(
    client, admin, database, source, authorized, monkeypatch, prowlarr_http
):
    await configure(client)
    prowlarr_http.update(
        releases=[
            release(
                title="Writer - Harbor (retail) (m4b)", protocol="usenet", categories=[{"id": 3030}]
            )
        ],
        bytes=nzb_bytes(name="Harbor", filename="Harbor.m4b"),
    )
    found = (await search(client)).json()["items"][0]
    assert found["release"]["formats"] == ["m4b"]
    inspected = await resolve(client, found["id"])
    assert inspected.status_code == 200, inspected.text
    source["artifact"] = UUID(inspected.json()["id"])
    source["release"] = ProwlarrRelease.model_validate(found["release"])
    async with database() as db, db.begin():
        result = await db.get(SourceResult, UUID(found["id"]))
        result.operation_id = authorized["search"]
        search_row = await db.get(Operation, authorized["search"])
        search_row.payload = {
            **search_row.payload,
            "workers": {},
            "sources": {},
            "query": "Harbor",
            "medium": "audio",
            "offset": 0,
        }
        downloader = await db.get(Integration, UUID(authorized["body"]["downloader_id"]))
        downloader.kind = "sabnzbd"
        downloader.encrypted_secrets = encrypt_secrets({"api_key": "private-sab-key"})
    await defaults(client, authorized)
    profiles = (await client.get("/api/acquisition/profiles")).json()
    async with database() as db, db.begin():
        search_row = await db.get(Operation, authorized["search"])
        search_row.payload = {**search_row.payload, "profile": profiles[0]}
    grab = Grab()
    monkeypatch.setattr(downloads, "SabClient", lambda *args, **kwargs: grab)
    path = f"/api/source-searches/{authorized['search']}/results/{found['id']}/download"
    response = await client.post(path, headers={"Idempotency-Key": "clicked-nzb"})
    assert response.status_code == 202, response.text
    operation_id = UUID(response.json()["id"])
    await automatic_selection.run(operation_id)
    async with database() as db:
        operation = await db.get(Operation, operation_id)
        assert operation.status == "completed", operation.message
        attempt_id = UUID(operation.payload["download_id"])
    await downloads.run(attempt_id)
    repeated = await client.post(path, headers={"Idempotency-Key": "clicked-nzb"})
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["id"] == str(operation_id)
    await downloads.run(attempt_id)
    assert grab.calls.count("submit") == 1
    async with database() as db:
        assert (await db.get(DownloadAttempt, attempt_id)).state == "downloading"


@pytest.mark.parametrize("redirect", [None, "https://indexer.test/file?apikey=secret-indexer"])
async def test_usenet_grab_is_sent_to_sabnzbd_once(
    client, admin, database, selection_route, monkeypatch, prowlarr_http, redirect
):
    await configure(client)
    prowlarr_http.update(
        releases=[release(protocol="usenet", categories=[{"id": 3030}])],
        bytes=nzb_bytes(),
        redirect=redirect,
    )
    item = (await search(client)).json()["items"][0]
    inspected = await resolve(client, item["id"])
    assert inspected.status_code == 200, inspected.text
    selection_route["artifact_id"] = inspected.json()["id"]
    async with database() as db, db.begin():
        downloader = await db.get(Integration, UUID(selection_route["downloader_id"]))
        downloader.kind = "sabnzbd"
        downloader.encrypted_secrets = encrypt_secrets({"api_key": "private-sab-key"})
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    grab = Grab()
    monkeypatch.setattr(downloads, "SabClient", lambda *args, **kwargs: grab)
    prepared = await prepare(client, selection_route)
    assert prepared.status_code == 201, prepared.text
    assert "private-sab-key" not in prepared.text
    saved = (await start(client, prepared.json())).json()
    await downloads.run(UUID(saved["id"]))
    assert grab.calls.count("submit") == 1
    async with database() as db:
        assert (await db.get(DownloadAttempt, UUID(saved["id"]))).state == "downloading"
    grab.complete = True
    await downloads.run(UUID(saved["id"]))
    assert grab.calls.count("submit") == 1
    async with database() as db:
        attempt = await db.get(DownloadAttempt, UUID(saved["id"]))
        inspection = await db.get(DownloadInspection, attempt.inspection_id)
        assert attempt.state == "complete"
        assert inspection.relative_path == "Finished Book"
