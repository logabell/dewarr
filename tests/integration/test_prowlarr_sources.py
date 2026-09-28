# ruff: noqa: F811
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.adapters.prowlarr import ProwlarrClient
from app.config import get_settings
from app.db.models import AcquisitionSelection, SourceArtifact, SourceConnection, SourceResult, User
from app.domain import download_attempts, prowlarr_network
from app.domain.source_artifacts import artifact_bytes
from app.security import decrypt_secrets
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401
from tests.integration.test_download_attempts import Client, row, start
from tests.prowlarr_fixture import indexer, release
from tests.torrent_fixture import torrent_bytes

pytestmark = pytest.mark.integration


@pytest.fixture
def prowlarr_http(monkeypatch):
    state = {
        "calls": [],
        "status": 200,
        "headers": {},
        "gate": None,
        "entered": asyncio.Event(),
        "indexers": [indexer()],
        "releases": [release()],
        "bytes": torrent_bytes(),
        "redirect": None,
    }

    async def handler(req):
        state["calls"].append(req)
        assert req.headers["x-api-key"] == "secret-api"
        state["entered"].set()
        if state["gate"]:
            await state["gate"].wait()
        if req.url.path.endswith("/download"):
            if state["redirect"]:
                return httpx.Response(301, headers={"Location": state["redirect"]})
            return httpx.Response(state["status"], content=state["bytes"], headers=state["headers"])
        body = (
            state["indexers"]
            if req.url.path.endswith("/indexer")
            else state["releases"]
            if req.url.path.endswith("/search")
            else {"version": "2.3.0"}
        )
        return httpx.Response(state["status"], json=body, headers=state["headers"])

    async def resolver(host, port):
        return ["93.184.216.34"]

    async def redirected(req):
        state["calls"].append(req)
        assert req.url.host == "93.184.216.34"
        assert req.headers["host"] == "indexer.test"
        assert "x-api-key" not in req.headers
        return httpx.Response(state["status"], content=state["bytes"])

    monkeypatch.setattr(
        prowlarr_network,
        "ProwlarrClient",
        lambda *args: ProwlarrClient(
            *args,
            transport=httpx.MockTransport(handler),
            redirect_transport=httpx.MockTransport(redirected),
            resolver=resolver,
        ),
    )
    monkeypatch.setattr(prowlarr_network, "REQUEST_INTERVAL", 0)
    return state


async def configure(client, generation=0, **changes):
    return await client.put(
        "/api/sources/prowlarr/connection",
        json={
            "base_url": "https://prowlarr.test/base",
            "api_key": "secret-api",
            "expected_generation": generation,
            **changes,
        },
    )


async def search(client, **changes):
    return await client.post(
        "/api/sources/prowlarr/search", json={"q": "Book", "indexer_id": 7, **changes}
    )


async def resolve(client, result_id):
    return await client.post(f"/api/sources/prowlarr/results/{result_id}/artifact")


@pytest.mark.parametrize("redirect", [None, "https://indexer.test/file?apikey=secret-indexer"])
async def test_private_search_resolution_and_shared_download_path(
    client, admin, database, selection_route, prowlarr_http, monkeypatch, caplog, redirect
):
    prowlarr_http["redirect"] = redirect
    caplog.set_level(logging.INFO, logger="httpx")
    assert (await configure(client)).status_code == 200
    assert (await client.post("/api/sources/prowlarr/connection/test")).json()[
        "status"
    ] == "connected"
    found = await search(client)
    assert found.status_code == 200, found.text
    result_id = found.json()["items"][0]["id"]
    inspected = await resolve(client, result_id)
    assert inspected.status_code == 200, inspected.text
    artifact_id = inspected.json()["id"]
    assert (await resolve(client, result_id)).json()["id"] == artifact_id
    async with database() as db:
        artifact = await db.get(SourceArtifact, UUID(artifact_id))
        assert artifact_bytes(artifact) == prowlarr_http["bytes"]
        saved = await db.get(SourceResult, UUID(result_id))
        assert "secret" not in saved.encrypted_reference
        assert decrypt_secrets(saved.encrypted_reference)["link"] == "secret_proxy_link"
    selection_route["artifact_id"] = artifact_id
    prepared = await prepare(client, selection_route)
    assert prepared.status_code == 201, prepared.text
    selected = prepared.json()
    async with database() as db:
        frozen = (await db.get(AcquisitionSelection, UUID(selected["id"]))).frozen
        assert frozen["release"]["source"] == "prowlarr"
        downloader = Client(database, frozen["descriptor"])
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    monkeypatch.setattr(download_attempts, "QbitClient", lambda *args: downloader)
    queued = await start(client, selected)
    assert queued.status_code == 202, queued.text
    await download_attempts.run(UUID(queued.json()["id"]))
    await download_attempts.run(UUID(queued.json()["id"]))
    assert downloader.calls.count("submit") == 1
    assert (await row(database, queued.json()["id"])).state == "downloading"
    assert "secret" not in found.text + inspected.text + prepared.text + caplog.text
    assert all(req.method == "GET" for req in prowlarr_http["calls"])
    # Duplicate release rows must not make a full upstream page appear terminal.
    prowlarr_http["releases"] = [release(), release()]
    page = (await search(client, limit=2)).json()
    assert len(page["items"]) == 1 and page["may_have_more"]


async def test_generation_expiry_private_owner_and_viewer_guards(
    client, admin, database, prowlarr_http
):
    await configure(client)
    item = (await search(client)).json()["items"][0]
    await configure(client, 1)
    assert (await resolve(client, item["id"])).status_code == 409
    fresh = (await search(client)).json()["items"][0]
    async with database() as db, db.begin():
        saved = await db.get(SourceResult, UUID(fresh["id"]))
        saved.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await resolve(client, fresh["id"])).status_code == 409
    fresh = (await search(client)).json()["items"][0]
    async with database() as db, db.begin():
        saved = await db.get(SourceResult, UUID(fresh["id"]))
        other = User(
            username="other", display_name="Other", password_hash="not-login", role="member"
        )
        db.add(other)
        await db.flush()
        saved.owner_id = other.id
    assert (await resolve(client, fresh["id"])).status_code == 404
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "viewer"
    assert (await resolve(client, fresh["id"])).status_code == 403
    assert (await configure(client, 2)).status_code == 403
    assert (await search(client)).status_code == 200


async def test_exclusion_native_overlap_and_capabilities(client, admin, database, prowlarr_http):
    await configure(client, excluded_indexers=[7])
    assert (await client.get("/api/sources/prowlarr/indexers")).json()[0]["excluded"]
    assert (await search(client)).status_code == 422
    assert not any(req.url.path.endswith("/search") for req in prowlarr_http["calls"])
    await configure(client, 1)
    prowlarr_http["indexers"] = [indexer(supportsPagination=False)]
    assert (await search(client, offset=50)).status_code == 422
    prowlarr_http["indexers"] = [indexer(definitionName="MyAnonamouse")]
    async with database() as db, db.begin():
        db.add(
            SourceConnection(
                key="mam", base_url="https://mam.test", encrypted_secrets="unused", enabled=True
            )
        )
    assert (await search(client)).status_code == 422
    assert (await client.get("/api/sources/prowlarr/indexers")).json()[0]["native_mam"]


async def test_inflight_settings_change_discards_results_and_cooldown_survives_edits(
    client, admin, database, prowlarr_http
):
    await configure(client)
    prowlarr_http["gate"] = asyncio.Event()
    task = asyncio.create_task(search(client))
    await asyncio.wait_for(prowlarr_http["entered"].wait(), 5)
    assert (await configure(client, 1)).status_code == 200
    prowlarr_http["gate"].set()
    assert (await task).status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SourceResult)) == 0
    prowlarr_http.update(gate=None, status=429, headers={"Retry-After": "60"})
    response = await search(client)
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
    await configure(client, 2)
    count = len(prowlarr_http["calls"])
    assert (await search(client)).status_code == 429
    assert len(prowlarr_http["calls"]) == count


async def test_expired_read_lease_recovers_and_revoked_actor_does_not_get_results(
    client, admin, database, prowlarr_http
):
    await configure(client)
    async with database() as db, db.begin():
        connection = await db.get(SourceConnection, "prowlarr")
        connection.lease_token = uuid4()
        connection.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert (await search(client)).status_code == 200
    prowlarr_http["entered"].clear()
    prowlarr_http["gate"] = asyncio.Event()
    task = asyncio.create_task(search(client))
    await asyncio.wait_for(prowlarr_http["entered"].wait(), 5)
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).active = False
    prowlarr_http["gate"].set()
    assert (await task).status_code == 401
    async with database() as db:
        assert (await db.get(SourceConnection, "prowlarr")).lease_token is None


@pytest.mark.parametrize("redirect", [None, "https://indexer.test/file?apikey=secret-indexer"])
async def test_unsupported_and_invalid_torrents_never_become_artifacts(
    client, admin, database, prowlarr_http, redirect
):
    prowlarr_http["redirect"] = redirect
    await configure(client)
    prowlarr_http["releases"] = [release(protocol="usenet")]
    result_id = (await search(client)).json()["items"][0]["id"]
    assert (await resolve(client, result_id)).status_code == 502
    prowlarr_http["releases"] = [release()]
    prowlarr_http["bytes"] = b"<html>login secret</html>"
    result_id = (await search(client)).json()["items"][0]["id"]
    assert (await resolve(client, result_id)).status_code == 502
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SourceArtifact)) == 0


async def test_active_lease_rejects_second_request_without_upstream_call(
    client, admin, prowlarr_http
):
    await configure(client)
    prowlarr_http["gate"] = asyncio.Event()
    task = asyncio.create_task(search(client))
    await asyncio.wait_for(prowlarr_http["entered"].wait(), 5)
    blocked = await search(client)
    assert blocked.status_code == 429
    assert len(prowlarr_http["calls"]) == 1
    prowlarr_http["gate"].set()
    assert (await task).status_code == 200


async def test_endpoint_change_requires_new_key_and_stale_settings_do_not_overwrite(
    client, admin, prowlarr_http
):
    saved = await configure(client)
    assert "secret-api" not in saved.text
    assert (await configure(client)).status_code == 409
    result = await client.put(
        "/api/sources/prowlarr/connection",
        json={"base_url": "https://other.test", "expected_generation": 1},
    )
    assert result.status_code == 422
    settings = (await client.get("/api/sources/prowlarr/connection")).json()
    assert settings["generation"] == 1 and settings["base_url"] == "https://prowlarr.test/base"
