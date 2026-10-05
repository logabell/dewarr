from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.adapters.catalog_types import BookData, EditionData
from app.adapters.contracts import AdapterError, FailureKind
from app.config import get_settings
from app.db.models import (
    DiscoveryFollow,
    Operation,
    ProviderCache,
    ProviderObject,
    User,
    Version,
    Work,
)
from app.domain.catalog_enrichment import enrich, schedule_enrichment
from app.domain.catalog_metadata import import_book
from app.domain.discovery_catalog import refresh, snapshot_key

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("locked,conflict", [(False, False), (True, False), (False, True)])
async def test_recording_worker_fills_exact_edition_without_replacing_work(
    database, admin, client, monkeypatch, locked, conflict
):
    primary = BookData(
        provider="hardcover",
        external_id="42",
        title="Harbor",
        authors=["Writer"],
        description="Primary synopsis",
        publication_year=2001,
        cover_url="https://assets.hardcover.app/cover.jpg",
        editions=[
            EditionData(
                external_id="410",
                medium="audio",
                title="Harbor",
                identifiers={"asin": "B012345678"},
                narrators=["Existing narrator"] if conflict else [],
            )
        ],
    )
    async with database() as db, db.begin():
        user = await db.get(User, UUID(admin["id"]))
        work = await import_book(db, user, primary)
        wid = work.id
        version = await db.scalar(select(Version).where(Version.work_id == wid))
        vid = version.id
        if locked:
            link = await db.scalar(select(ProviderObject).where(ProviderObject.version_id == vid))
            link.manual_lock = True
        operation = await schedule_enrichment(db, user, work)
        oid = operation.id

    async def recording(identifier):
        assert identifier == "B012345678"
        return BookData(
            provider="audible",
            external_id=identifier,
            title="Harbor",
            authors=["Writer"],
            description="Recording synopsis",
            editions=[
                EditionData(
                    external_id=identifier,
                    medium="audio",
                    title="Harbor",
                    language="en",
                    identifiers={"asin": identifier},
                    narrators=["Audio Narrator"],
                    runtime_minutes=620,
                    publication_year=2026,
                    abridged=False,
                )
            ],
        )

    monkeypatch.setattr("app.adapters.audible.recording", recording)
    await enrich(oid)
    await enrich(oid)  # Redelivery is idempotent.
    async with database() as db:
        assert (await db.get(Operation, oid)).status == "completed"
        work = await db.get(Work, wid)
        assert work.description == "Primary synopsis" and work.publication_year == 2001
        assert await db.scalar(select(func.count()).select_from(Version)) == 1
        version = await db.get(Version, vid)
        assert version.runtime_minutes == (None if locked or conflict else 620)
        if conflict:
            assert version.narrators == ["Existing narrator"]
        link = await db.scalar(
            select(ProviderObject).where(ProviderObject.provider.like("audible:%"))
        )
        assert link.match_status == ("needs-review" if conflict else "matched")
    view = await client.get(f"/api/metadata/works/{wid}")
    assert view.status_code == 200, view.text
    assert {s["provider"] for s in view.json()["sources"]} == {"hardcover", "audible"}
    assert len(view.json()["versions"]) == 1
    if not locked and not conflict:
        source = next(s for s in view.json()["sources"] if s["provider"] == "audible")
        detached = await client.post(
            f"/api/identity/sources/{source['id']}/unmatch",
            json={"expected_revision": source["revision"]},
        )
        assert detached.status_code == 204, detached.text
        async with database() as db:
            version = await db.get(Version, vid)
            assert version.narrators == [] and version.runtime_minutes is None
        changes = (await client.get("/api/identity/changes", params={"work_id": str(wid)})).json()
        undone = await client.post(f"/api/identity/changes/{changes['items'][0]['id']}/undo")
        assert undone.status_code == 204, undone.text
        async with database() as db:
            assert (await db.get(Version, vid)).runtime_minutes == 620
        current = (await client.get(f"/api/metadata/works/{wid}")).json()
        source = next(s for s in current["sources"] if s["provider"] == "audible")
        detached = await client.post(
            f"/api/identity/sources/{source['id']}/unmatch",
            json={"expected_revision": source["revision"]},
        )
        assert detached.status_code == 204
        from app.domain.catalog_metadata import attach_source

        async with database() as db, db.begin():
            await attach_source(
                db, await db.get(Work, wid), await recording("B012345678"), explicit=True
            )
        async with database() as db:
            version = await db.get(Version, vid)
            assert version.narrators == ["Audio Narrator"] and version.runtime_minutes == 620


async def test_official_awards_browse_follow_and_recording_review(client, admin, database):
    index = await client.get("/api/discovery/collections?kind=award&provider=ala&audience=children")
    assert index.status_code == 200, index.text
    assert {c["title"] for c in index.json()["items"]} == {
        "Newbery Medal & Honors",
        "Caldecott Medal & Honors",
    }
    key = "official-audie-audiobook-2025"
    detail = (await client.get(f"/api/discovery/collections/{key}")).json()
    assert detail["items"][0]["narrators"] == ["Barbra Streisand"]
    eid = detail["items"][0]["external_id"]
    resolved = (await client.get(f"/api/discovery/curation/{eid}")).json()
    assert resolved["match"]["status"] == "needs-review" and not resolved["match"]["book"]
    for _ in range(2):
        followed = await client.put(
            f"/api/discovery/collections/{key}/follow", json={"pinned": True, "tracking": True}
        )
        assert followed.status_code == 200, followed.text
    assert followed.json()["refresh_mode"] == "app-update"
    await refresh(UUID(admin["id"]), key, 2)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DiscoveryFollow)) == 1
        assert await db.scalar(select(func.count()).select_from(Work)) == 0
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0
    winners = (await client.get(f"/api/discovery/collections/{key}?winners=true")).json()
    assert winners["total"] == 1


async def test_nyt_live_collection_refresh_failure_keeps_ranked_snapshot(
    client, admin, database, monkeypatch, caplog
):
    import logging

    caplog.set_level(logging.INFO, logger="httpx")
    monkeypatch.setattr(get_settings(), "nyt_api_key", SecretStr("private-nyt-key"))
    from app.domain.catalog_network import CatalogGateway

    requests = []

    async def respond(request):
        requests.append(request)
        assert request.url.params["api-key"] == "private-nyt-key"
        assert not request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "results": {
                    "published_date": "2026-10-04",
                    "books": [
                        {
                            "title": "HARBOR",
                            "author": "Writer",
                            "primary_isbn13": "9781234567897",
                            "rank": 1,
                        },
                    ],
                }
            },
        )

    class Gateway(CatalogGateway):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("app.domain.curation_sources.CatalogGateway", Gateway)
    key = "nyt-hardcover-fiction"
    response = await client.get(f"/api/discovery/collections/{key}")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["rank"] == 1
    assert response.json()["collection"]["edition_date"] == "2026-10-04"
    await client.put(
        f"/api/discovery/collections/{key}/follow", json={"pinned": True, "tracking": True}
    )
    assert len(requests) == 1
    assert "private-nyt-key" not in caplog.text

    async def failure(value):
        raise AdapterError(FailureKind.UNAVAILABLE, "private upstream detail")

    monkeypatch.setattr("app.domain.curation_sources.fetch", failure)
    await refresh(UUID(admin["id"]), key, 1)
    async with database() as db, db.begin():
        row = await db.get(DiscoveryFollow, (UUID(admin["id"]), key))
        assert row.error and row.snapshot["books"][0]["rank"] == 1
        cache = await db.get(ProviderCache, snapshot_key(key))
        cache.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    response = await client.get(f"/api/discovery/collections/{key}")
    assert response.status_code == 200
    assert response.json()["collection"]["warning"]
    assert "private" not in response.text


async def test_custom_source_search_preview_import_and_missing_configuration(
    client, admin, database, monkeypatch
):
    from app.domain.catalog_network import CatalogGateway

    monkeypatch.setattr(get_settings(), "custom_metadata_url", "https://regional.example")
    monkeypatch.setattr(get_settings(), "custom_metadata_token", SecretStr("regional-token"))

    async def respond(request):
        assert request.url.host == "regional.example" and request.url.path == "/search"
        assert request.headers["authorization"] == "Bearer regional-token"
        return httpx.Response(
            200,
            json={
                "matches": [
                    {
                        "title": "Пикник на обочине",
                        "author": "Аркадий и Борис Стругацкие",
                        "language": "Russian",
                        "description": "Regional synopsis",
                        "isbn": "9785170903427",
                    },
                ]
            },
        )

    class Gateway(CatalogGateway):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("app.api.metadata.CatalogGateway", Gateway)
    response = await client.get("/api/metadata/search?provider=custom&q=Пикник")
    assert response.status_code == 200, response.text
    book = response.json()["items"][0]
    assert book["language"] == "ru"
    path = f"/api/metadata/books/custom/{book['external_id']}"
    assert (await client.get(path)).json()["book"]["title"] == book["title"]
    added = await client.post(path + "/import")
    assert added.status_code == 200, added.text
    again = await client.post(path + "/import")
    assert again.json()["id"] == added.json()["id"]
    monkeypatch.setattr(get_settings(), "custom_metadata_url", None)
    assert (await client.get(path)).status_code == 503


async def test_audio_import_fallback_requires_exact_identifier_and_compatible_evidence(monkeypatch):
    from app.importing.catalog_resolution import lookup
    from app.importing.match_evidence import IdentifierEvidence, MatchEvidence

    async def recording(identifier):
        return BookData(
            provider="audible",
            external_id=identifier,
            title="Harbor",
            authors=["Writer"],
            editions=[
                EditionData(
                    external_id=identifier,
                    medium="audio",
                    title="Harbor",
                    narrators=["Narrator"],
                    identifiers={"asin": identifier},
                )
            ],
        )

    monkeypatch.setattr("app.adapters.audible.recording", recording)
    inputs = {
        "settings": {"primary": "hardcover"},
        "sources": [],
        "title": "Harbor",
        "authors": ["Writer"],
    }
    facts = MatchEvidence(
        titles=["harbor"],
        authors=[["writer"]],
        narrators=[["narrator"]],
        identifiers=[IdentifierEvidence(namespace="asin", value="B012345678")],
    )
    book, status, _ = await lookup(inputs, facts, "audio", None)
    assert book and status == "completed"
    facts.narrators = [["wrong narrator"]]
    book, status, _ = await lookup(inputs, facts, "audio", None)
    assert not book and status == "needs-review"


async def test_followed_live_chart_uses_newest_snapshot_and_refreshes_daily(
    client, admin, database, monkeypatch
):
    from copy import deepcopy

    from app.domain.curation_sources import audible_collections
    from app.domain.discovery_catalog import save_snapshot

    key = "audible-us-popular"
    old = audible_collections()[key]
    old.update(
        updated_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
        books=[
            dict(
                provider="audible",
                external_id="B012345678",
                title="Old selection",
                authors=["Writer"],
                identifiers={},
                subject="recording",
            )
        ],
        count=1,
    )
    async with database() as db, db.begin():
        db.add(
            DiscoveryFollow(
                user_id=UUID(admin["id"]),
                collection_id=key,
                snapshot=old,
                pinned=True,
                tracking=True,
                generation=1,
                next_check_at=datetime.now(UTC),
                error="Earlier refresh failed",
            )
        )
    current = deepcopy(old)
    current["updated_at"] = datetime.now(UTC).isoformat()
    current["books"][0]["title"] = "Current selection"
    await save_snapshot(current)
    for path in (f"/api/discovery/collections/{key}", "/api/discovery/collections?saved=true"):
        response = await client.get(path)
        assert response.status_code == 200, response.text
        if "collection" in response.json():
            assert response.json()["items"][0]["title"] == "Current selection"
            assert response.json()["collection"]["warning"] is None
        else:
            assert response.json()["items"][0]["updated_at"] == current["updated_at"].replace(
                "+00:00", "Z"
            )
    later = deepcopy(current)
    later["updated_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    later["books"][0]["title"] = "Worker refreshed selection"

    async def fresh(value):
        return later

    monkeypatch.setattr("app.domain.curation_sources.fetch", fresh)
    await refresh(UUID(admin["id"]), key, 1)
    async with database() as db:
        row = await db.get(DiscoveryFollow, (UUID(admin["id"]), key))
        assert timedelta(hours=23) < row.next_check_at - datetime.now(UTC) < timedelta(hours=25)
    response = await client.get(f"/api/discovery/collections/{key}")
    assert response.json()["items"][0]["title"] == "Worker refreshed selection"
    followed = await client.put(
        f"/api/discovery/collections/{key}/follow", json={"pinned": False, "tracking": False}
    )
    assert followed.status_code == 200
    async with database() as db:
        row = await db.get(DiscoveryFollow, (UUID(admin["id"]), key))
        assert row.snapshot["books"][0]["title"] == "Worker refreshed selection"


async def test_award_manifests_update_saved_collections_without_cross_kind_facets(
    client, admin, database
):
    from copy import deepcopy

    from app.domain.discovery_catalog import catalog

    key = "official-audie-audiobook-2025"
    old = deepcopy(catalog()[key])
    old["title"] = "Outdated award title"
    async with database() as db, db.begin():
        db.add(
            DiscoveryFollow(
                user_id=UUID(admin["id"]),
                collection_id=key,
                snapshot=old,
                pinned=True,
                tracking=False,
                next_check_at=datetime.now(UTC),
                generation=1,
            )
        )
    response = await client.get(f"/api/discovery/collections/{key}")
    assert response.json()["collection"]["title"] == catalog()[key]["title"]
    response = await client.get("/api/discovery/collections?kind=award")
    assert "audible" not in response.json()["providers"]
    winners = await client.get(
        "/api/discovery/browse?provider=audie&winners=true&language=en&audience=adult"
    )
    assert winners.status_code == 200
    assert winners.json()["items"]
    assert {b["provider"] for b in winners.json()["items"]} == {"audie"}
    excluded = await client.get("/api/discovery/browse?provider=audie&audience=children")
    assert excluded.json()["total"] == 0
    await refresh(UUID(admin["id"]), key, 1)
    assert catalog()[key]["title"] != old["title"]
