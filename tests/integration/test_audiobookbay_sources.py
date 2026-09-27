# ruff: noqa: F811
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from app.adapters.audiobookbay import ABBClient
from app.adapters.qbittorrent import QbitClient
from app.adapters.torrent_descriptor import inspect_torrent
from app.config import get_settings
from app.db.models import Integration, SourceArtifact, SourceConnection, SourceResult, User
from app.domain import audiobookbay_network, book_sources, download_attempts, downloaders
from app.domain import automatic_selection as automatic
from app.domain.source_artifacts import artifact_bytes
from app.security import decrypt_secrets
from tests import abb_fixture
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401
from tests.integration.test_book_sources import begin, read
from tests.integration.test_download_attempts import Client, row, start
from tests.torrent_fixture import torrent_bytes

pytestmark = pytest.mark.integration


@pytest.fixture
async def abb_http(monkeypatch):
    raw = torrent_bytes(name=b"Harbor", files=[{b"length": 12, b"path": [b"Harbor.m4b"]}])
    descriptor = await inspect_torrent(raw)
    state = {
        "calls": [],
        "qbit_calls": [],
        "status": 200,
        "headers": {},
        "gate": None,
        "entered": asyncio.Event(),
        "raw": raw,
        "descriptor": descriptor,
        "qbit_gate": None,
        "qbit_entered": asyncio.Event(),
        "version": "v5.2.3",
        "malformed": False,
    }

    async def handler(request):
        state["calls"].append(request)
        state["entered"].set()
        if state["gate"]:
            await state["gate"].wait()
        if state.get("redirect_capitals") and request.url.params.get(
            "s", ""
        ).lower() != request.url.params.get("s", ""):
            return httpx.Response(302, headers={"location": "/"})
        html = (
            abb_fixture.detail(digest=state["descriptor"].infohash_v1)
            if request.url.path == abb_fixture.PATH
            else abb_fixture.search()
        )
        html = (
            "<html>changed layout</html>"
            if state["malformed"]
            else html.replace("Alex Morgan", "Writer").replace("1.5 GBs", "12 Bytes")
        )
        for before, after in state.get("replacements", {}).items():
            html = html.replace(before, after)
        return httpx.Response(
            state["status"], text=html, headers={"content-type": "text/html", **state["headers"]}
        )

    async def qbit_handler(request):
        action = request.url.path.rsplit("/", 1)[-1]
        state["qbit_calls"].append(action)
        if action in {"login", "version", "webapiVersion"}:
            return httpx.Response(
                200,
                text={"login": "Ok.", "version": state["version"], "webapiVersion": "2.15.1"}[
                    action
                ],
            )
        state["qbit_entered"].set()
        if state["qbit_gate"]:
            await state["qbit_gate"].wait()
        if action == "fetchMetadata":
            return httpx.Response(200, json={"hash": state["descriptor"].infohash_v1, "info": {}})
        if action == "saveMetadata":
            return httpx.Response(200, content=state["raw"])
        raise AssertionError("Metadata inspection must not dispatch: " + action)

    monkeypatch.setattr(
        audiobookbay_network,
        "ABBClient",
        lambda *args, **kwargs: ABBClient(
            *args, **kwargs, request_interval=0, transport=httpx.MockTransport(handler)
        ),
    )
    monkeypatch.setattr(audiobookbay_network, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(
        downloaders,
        "QbitClient",
        lambda *args: QbitClient(*args, transport=httpx.MockTransport(qbit_handler)),
    )
    monkeypatch.setattr(downloaders, "TEST_INTERVAL", 0)
    return state


async def configure(client, generation=0, **values):
    return await client.put(
        "/api/sources/audiobookbay/connection",
        json={"base_url": abb_fixture.ORIGIN, "expected_generation": generation, **values},
    )


async def search(client):
    return await client.post("/api/sources/audiobookbay/search", json={"q": "Harbor"})


async def resolve(client, result_id):
    return await client.post(f"/api/sources/audiobookbay/results/{result_id}/artifact")


async def test_abb_real_metadata_enters_existing_manual_selection_and_one_transfer(
    client, admin, database, selection_route, abb_http, monkeypatch
):
    saved = await configure(client, metadata_downloader_id=selection_route["downloader_id"])
    assert saved.status_code == 200, saved.text
    assert (await client.post("/api/sources/audiobookbay/connection/test")).json()[
        "status"
    ] == "connected"
    found = await search(client)
    assert found.status_code == 200, found.text
    result_id = found.json()["items"][0]["id"]
    detail = await client.get(f"/api/sources/audiobookbay/results/{result_id}")
    assert detail.json()["files"][0]["evidence"] == "claimed"
    assert detail.json()["seeders"] is None and not detail.json()["metadata_resolved"]
    inspected = await resolve(client, result_id)
    assert inspected.status_code == 200, inspected.text
    artifact_id = inspected.json()["id"]
    assert inspected.json()["release"]["metadata_resolved"]
    assert (await resolve(client, result_id)).json()["id"] == artifact_id
    async with database() as db:
        assert artifact_bytes(await db.get(SourceArtifact, UUID(artifact_id))) == abb_http["raw"]
    assert set(abb_http["qbit_calls"]) == {
        "login",
        "version",
        "webapiVersion",
        "fetchMetadata",
        "saveMetadata",
    }
    prepared = await prepare(client, {**selection_route, "artifact_id": artifact_id})
    assert prepared.status_code == 201, prepared.text
    downloader = Client(database, inspected.json()["descriptor"])
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    monkeypatch.setattr(download_attempts, "QbitClient", lambda *args: downloader)
    queued = await start(client, prepared.json())
    assert queued.status_code == 202, queued.text
    await download_attempts.run(UUID(queued.json()["id"]))
    await download_attempts.run(UUID(queued.json()["id"]))
    assert downloader.calls.count("submit") == 1
    assert (await row(database, queued.json()["id"])).state == "downloading"
    assert "private-password" not in found.text + detail.text + inspected.text


async def test_combined_search_and_opt_in_automatic_selection_use_chosen_downloader(
    client, admin, database, selection_route, catalog, abb_http
):
    # Deliberately no global resolver: automatic selection uses its approved route.
    await configure(client)
    async with database() as db, db.begin():
        (await db.get(SourceConnection, "mam")).enabled = False
    profile = await client.post(
        "/api/acquisition/profiles",
        json={
            "name": "Metadata availability",
            "preferences": {
                "allow_unknown_seeders": True,
                "source_order": ["mam", "audiobookbay", "prowlarr"],
            },
        },
    )
    assert profile.status_code == 201, profile.text
    saved = await begin(
        client, catalog, medium="audio", profile_id=profile.json()["id"], profile_generation=1
    )
    await book_sources.run(UUID(saved["id"]), "audiobookbay")
    found = (await read(client, saved["id"])).json()
    assert found["status"] == "completed" and len(found["items"]) == 1
    assert found["items"][0]["release"]["source"] == "audiobookbay"
    body = {
        k: v for k, v in selection_route.items() if k not in {"artifact_id", "confirmed_work_id"}
    }
    body["search_id"] = saved["id"]
    started = await client.post(
        "/api/acquisition/automatic-selections",
        json=body,
        headers={"Idempotency-Key": "abb-auto-selection"},
    )
    assert started.status_code == 202, started.text
    await automatic.run(UUID(started.json()["id"]))
    value = await client.get(f"/api/acquisition/automatic-selections/{started.json()['id']}")
    assert value.json()["status"] == "completed", value.text
    assert value.json()["selection_id"] and value.json()["inspections"] == 1
    assert "fetchMetadata" in abb_http["qbit_calls"] and "add" not in abb_http["qbit_calls"]


async def test_ebook_search_does_not_query_audio_only_source(client, admin, catalog, abb_http):
    await configure(client)
    saved = await begin(client, catalog, medium="ebook")
    await book_sources.run(UUID(saved["id"]), "audiobookbay")
    found = (await read(client, saved["id"])).json()
    assert found["status"] == "completed" and not found["items"]
    assert not abb_http["calls"]


@pytest.mark.parametrize("change", ["source", "downloader", "viewer", "inactive"])
async def test_inflight_metadata_fences_settings_and_actor_before_artifact(
    client, admin, database, selection_route, abb_http, change
):
    await configure(client, metadata_downloader_id=selection_route["downloader_id"])
    result_id = (await search(client)).json()["items"][0]["id"]
    abb_http["qbit_gate"] = asyncio.Event()
    task = asyncio.create_task(resolve(client, result_id))
    await asyncio.wait_for(abb_http["qbit_entered"].wait(), 5)
    async with database() as db, db.begin():
        if change == "source":
            (await db.get(SourceConnection, "audiobookbay")).generation += 1
        elif change == "downloader":
            (
                await db.get(Integration, UUID(selection_route["downloader_id"]))
            ).credential_generation += 1
        else:
            user = await db.get(User, UUID(admin["id"]))
            if change == "viewer":
                user.role = "viewer"
            else:
                user.active = False
    abb_http["qbit_gate"].set()
    response = await task
    assert (
        response.status_code
        == {"source": 409, "downloader": 409, "viewer": 403, "inactive": 401}[change]
    ), response.text
    async with database() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SourceArtifact)
                .where(SourceArtifact.source_key == "audiobookbay")
            )
            == 0
        )


async def test_generation_expiry_missing_resolver_and_client_capability(
    client, admin, database, selection_route, abb_http
):
    await configure(client)
    item = (await search(client)).json()["items"][0]
    assert (await resolve(client, item["id"])).status_code == 409
    await configure(client, 1, metadata_downloader_id=selection_route["downloader_id"])
    assert (await resolve(client, item["id"])).status_code == 409
    item = (await search(client)).json()["items"][0]
    abb_http["version"] = "v5.1.4"
    assert (await resolve(client, item["id"])).status_code == 422
    assert "fetchMetadata" not in abb_http["qbit_calls"]
    async with database() as db, db.begin():
        (await db.get(SourceResult, UUID(item["id"]))).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert (await resolve(client, item["id"])).status_code == 409


async def test_source_lease_cooldown_and_parser_errors_are_not_empty_success(
    client, admin, database, abb_http
):
    await configure(client)
    abb_http["gate"] = asyncio.Event()
    task = asyncio.create_task(search(client))
    await asyncio.wait_for(abb_http["entered"].wait(), 5)
    assert (await search(client)).status_code == 429
    assert (await configure(client, 1)).status_code == 200
    abb_http["gate"].set()
    assert (await task).status_code == 409
    abb_http.update(gate=None, status=429, headers={"Retry-After": "60"})
    limited = await search(client)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
    await configure(client, 2)
    count = len(abb_http["calls"])
    assert (await search(client)).status_code == 429 and len(abb_http["calls"]) == count
    async with database() as db, db.begin():
        (await db.get(SourceConnection, "audiobookbay")).blocked_until = None
    abb_http.update(status=200, headers={}, malformed=True)
    assert (await search(client)).status_code == 502
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SourceResult)) == 0


async def test_private_postings_and_viewer_permission_before_network(
    client, admin, database, abb_http
):
    await configure(client)
    item = (await search(client)).json()["items"][0]
    async with database() as db, db.begin():
        other = User(username="other", display_name="Other", password_hash="unused", role="member")
        db.add(other)
        await db.flush()
        (await db.get(SourceResult, UUID(item["id"]))).owner_id = other.id
    count = len(abb_http["calls"])
    assert (await client.get(f"/api/sources/audiobookbay/results/{item['id']}")).status_code == 404
    assert (await resolve(client, item["id"])).status_code == 404
    assert len(abb_http["calls"]) == count and not abb_http["qbit_calls"]
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "viewer"
    assert (await configure(client, 1)).status_code == 403
    assert (await client.get("/api/sources/audiobookbay/connection")).status_code == 403
    item = (await search(client)).json()["items"][0]
    assert (await client.get(f"/api/sources/audiobookbay/results/{item['id']}")).status_code == 200
    assert (await resolve(client, item["id"])).status_code == 403
    assert not abb_http["qbit_calls"]


async def test_proxy_credentials_are_preserved_only_on_same_route(client, admin, database):
    proxy = "http://proxy.test:8888"
    saved = await configure(
        client, proxy_url=proxy, proxy_username="fixture-user", proxy_password="fixture-secret"
    )
    assert saved.status_code == 200 and saved.json()["has_proxy_credentials"]
    assert "fixture-secret" not in saved.text and saved.json()["route"] == "required-proxy"
    assert (await configure(client, 1, proxy_url=proxy)).json()["has_proxy_credentials"]
    async with database() as db:
        secrets = decrypt_secrets(
            (await db.get(SourceConnection, "audiobookbay")).encrypted_secrets
        )
        assert secrets["proxy_password"] == "fixture-secret"
    changed = await configure(client, 2, proxy_url="http://other-proxy.test:8888")
    assert changed.status_code == 200 and not changed.json()["has_proxy_credentials"]
    assert (await configure(client, 2, proxy_url=proxy)).status_code == 409
    removed = await configure(client, 3)
    assert removed.json()["route"] == "direct" and not removed.json()["has_proxy_credentials"]
    invalid = await configure(client, 4, proxy_password="fixture-secret")
    assert invalid.status_code == 422
