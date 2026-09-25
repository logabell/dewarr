from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, func, select, update

from app.adapters.hardcover_discovery import (
    HC_DISCOVERY_BOOKS,
    HC_RECENT,
    HC_RELATED,
    HC_TRENDING,
)
from app.db.models import (
    AcquisitionIntent,
    AssetContains,
    CatalogAccount,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    Operation,
    ProviderCache,
    Work,
    WorkMetadataSource,
)
from app.domain.catalog_network import CatalogGateway
from tests.integration.test_metadata import connect

pytestmark = pytest.mark.integration


@pytest.fixture
def provider(monkeypatch):
    from app.api import metadata

    state = {"calls": [], "failure": None, "ids": [42, 43], "hook": None}

    async def respond(request):
        import json

        body = json.loads(request.content)
        state["calls"].append(body)
        if state["hook"]:
            hook, state["hook"] = state["hook"], None
            await hook()
        if state["failure"]:
            return httpx.Response(state["failure"], headers={"Retry-After": "120"}, json={})
        query, variables = body["query"], body["variables"]
        if query == HC_TRENDING:
            value = {"books_trending": {"ids": state["ids"]}}
        elif query == HC_DISCOVERY_BOOKS:
            value = {
                "books": [
                    {
                        "id": key,
                        "title": f"Provider Book {key}",
                        "cached_contributors": [{"author": {"name": "Shared Author"}}],
                    }
                    for key in reversed(variables["ids"])
                ]
            }
        elif query == HC_RECENT:
            value = {"books": [{"id": 44, "title": "New Release", "release_date": variables["to"]}]}
        elif query == HC_RELATED:
            value = {"books": [{"id": variables["id"], "cached_similar_book_ids": [43]}]}
        else:
            raise AssertionError(query)
        return httpx.Response(200, json={"data": value})

    class FixtureGateway(CatalogGateway):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(respond))

        async def reserve(self):
            pass

    monkeypatch.setattr(metadata, "CatalogGateway", FixtureGateway)
    return state


async def add_work(db, external_id=None, **values):
    work = Work(title="Local protected title", authors=["Shared Author"], **values)
    db.add(work)
    await db.flush()
    if external_id:
        db.add(
            WorkMetadataSource(
                work_id=work.id,
                provider="hardcover",
                external_id=external_id,
                fetched_at=datetime.now(UTC),
                snapshot={
                    "provider": "hardcover",
                    "external_id": external_id,
                    "title": work.title,
                    "authors": work.authors,
                },
            )
        )
        await db.flush()
    return work


async def add_owned(db, work):
    integration = Integration(
        kind="audiobookshelf",
        name="Private backend",
        base_url="http://fixture.invalid",
        encrypted_secrets="unused",
    )
    db.add(integration)
    await db.flush()
    library = Library(integration_id=integration.id, external_id="one", name="Private library")
    db.add(library)
    await db.flush()
    asset = LibraryAsset(
        library_id=library.id, external_id="one", medium="ebook", state="present", full_content=True
    )
    db.add(asset)
    await db.flush()
    db.add(AssetContains(asset_id=asset.id, work_id=work.id, verified=True))
    return library


async def login_member(client, role="member"):
    response = await client.post(
        "/api/auth/users",
        json={
            "username": "reader",
            "display_name": "Reader",
            "password": "long enough password",
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    uid = response.json()["id"]
    await client.post("/api/auth/logout")
    response = await client.post(
        "/api/auth/login", json={"username": "reader", "password": "long enough password"}
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return UUID(uid)


async def test_browsing_is_read_only_and_uses_known_identity_and_scoped_availability(
    client, admin, database, provider
):
    await connect(client)
    async with database() as db, db.begin():
        work = await add_work(db, "42")
        await add_owned(db, work)
        work_id = str(work.id)
    result = await client.get("/api/discovery/hardcover/trending")
    assert result.status_code == 200, result.text
    shelf = result.json()
    assert [item["book"]["external_id"] for item in shelf["items"]] == ["42", "43"]
    known, unknown = shelf["items"]
    assert known["work"]["id"] == work_id
    assert known["work"]["title"] == "Local protected title"
    assert known["work"]["availability"]["owned"] and known["work"]["availability"]["ebook"]
    assert unknown["work"] is None
    assert len(provider["calls"]) == 2
    again = await client.get("/api/discovery/hardcover/trending")
    assert again.json() == shelf and len(provider["calls"]) == 2
    assert (await client.get("/api/discovery/hardcover/new-releases")).json()["items"][0]["book"][
        "release_date"
    ]
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Work)) == 1
        for model in [AcquisitionIntent, Operation]:
            assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_discovery_preserves_private_metadata_and_library_grants(
    client, admin, database, provider
):
    async with database() as db, db.begin():
        private = await add_work(db, "42", catalog_public=False, catalog_owner_id=UUID(admin["id"]))
        private.title = "Private list title"
        public = await add_work(db, "43")
        library = await add_owned(db, public)
        library_id, work_id = library.id, public.id
    uid = await login_member(client)
    await connect(client)
    result = await client.get("/api/discovery/hardcover/trending")
    assert "Private list title" not in result.text
    assert result.json()["items"][0]["work"] is None
    assert not result.json()["items"][1]["work"]["availability"]["owned"]
    assert str(private.id) not in (await client.get("/api/discovery/local")).text
    async with database() as db, db.begin():
        db.add(LibraryGrant(library_id=library_id, user_id=uid))
    result = await client.get("/api/discovery/hardcover/trending")
    assert result.json()["items"][1]["work"]["availability"]["owned"]
    async with database() as db, db.begin():
        await db.execute(delete(LibraryGrant))
    result = await client.get("/api/discovery/hardcover/trending")
    assert not result.json()["items"][1]["work"]["availability"]["owned"]
    assert result.json()["items"][1]["work"]["id"] == str(work_id)
    assert len(provider["calls"]) == 2  # Inventory is fresh even when provider metadata is cached.


async def test_canonical_identity_deduplicates_but_ambiguous_source_matches_do_not_guess(
    client, admin, database, provider
):
    await connect(client)
    async with database() as db, db.begin():
        first = await add_work(db, "42")
        second = await add_work(db, "43")
        second.redirect_to = first.id
        root = first.id
    result = (await client.get("/api/discovery/hardcover/trending")).json()
    assert len(result["items"]) == 1 and result["items"][0]["work"]["id"] == str(root)
    async with database() as db, db.begin():
        await add_work(db, "42")
    result = (await client.get("/api/discovery/hardcover/trending")).json()
    assert result["items"][0]["work"] is None


async def test_unaccepted_source_and_title_similarity_do_not_assert_a_library_match(
    client, admin, database, provider
):
    await connect(client)
    async with database() as db, db.begin():
        work = await add_work(db, "42")
        work.title = "Provider Book 42"
        await add_owned(db, work)
        await db.execute(update(WorkMetadataSource).values(accepted=False))
    result = (await client.get("/api/discovery/hardcover/trending")).json()
    assert all(item["work"] is None for item in result["items"])


@pytest.mark.parametrize("failure", [503, 429])
async def test_cached_shelves_survive_transient_outage_with_truthful_stale_label(
    client, admin, database, provider, failure
):
    await connect(client)
    assert (await client.get("/api/discovery/hardcover/trending")).json()["items"]
    async with database() as db, db.begin():
        await db.execute(
            update(ProviderCache).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    provider["failure"] = failure
    result = (await client.get("/api/discovery/hardcover/trending")).json()
    assert result["stale"] and result["items"] and "cached" in result["warning"]


@pytest.mark.parametrize("failure", [401, 403])
async def test_auth_failure_does_not_reuse_cached_private_provider_data(
    client, admin, database, provider, failure
):
    await connect(client)
    await client.get("/api/discovery/hardcover/trending")
    async with database() as db, db.begin():
        await db.execute(
            update(ProviderCache).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    provider["failure"] = failure
    pending = (await client.get("/api/discovery/hardcover/trending")).json()
    assert pending["stale"] and pending["items"]
    from app.db.models import Operation
    from app.domain.catalog_refresh import run

    async with database() as db:
        job = await db.scalar(select(Operation).where(Operation.kind == "catalog.refresh"))
    await run(job.id)
    result = (await client.get("/api/discovery/hardcover/trending")).json()
    assert result["status"] == "unavailable" and not result["items"]
    assert not result["stale"]


async def test_disconnected_and_failing_shelves_leave_local_browsing_usable(
    client, admin, database, provider
):
    async with database() as db, db.begin():
        work = await add_work(db)
        work_id = str(work.id)
    disconnected = (await client.get("/api/discovery/hardcover/trending")).json()
    assert disconnected["status"] == "not-connected" and provider["calls"] == []
    await connect(client)
    provider["failure"] = 503
    unavailable = (await client.get("/api/discovery/hardcover/trending")).json()
    assert unavailable["status"] == "unavailable" and unavailable["warning"]
    local = (await client.get("/api/discovery/local")).json()
    assert local["items"][0]["work"]["id"] == work_id
    assert local["items"][0]["book"]["provider"] == "local"


async def test_account_generation_changes_fence_an_inflight_shelf(
    client, admin, database, provider
):
    await connect(client)

    async def change():
        async with database() as db, db.begin():
            account = await db.get(CatalogAccount, UUID(admin["id"]))
            account.generation += 1

    provider["hook"] = change
    response = await client.get("/api/discovery/hardcover/trending")
    assert response.status_code == 409 and "connection changed" in response.text


async def test_related_uses_attributed_provider_suggestions_and_local_fallback(
    client, admin, database, provider
):
    await connect(client)
    async with database() as db, db.begin():
        seed, other = await add_work(db, "42"), await add_work(db, "43")
        seed_id, other_id = str(seed.id), str(other.id)
    result = (await client.get(f"/api/discovery/related/{seed_id}")).json()
    assert result["items"][0]["work"]["id"] == other_id
    assert result["items"][0]["reason"] == "Suggested by Hardcover"
    async with database() as db, db.begin():
        await db.execute(delete(ProviderCache))
    provider["failure"] = 503
    result = (await client.get(f"/api/discovery/related/{seed_id}")).json()
    assert result["warning"] and not result["stale"]
    assert result["items"][0]["work"]["id"] == other_id
    assert result["items"][0]["reason"] == "Shares an author with this book"


async def test_related_private_seed_and_candidates_remain_private(
    client, admin, database, provider
):
    async with database() as db, db.begin():
        seed = await add_work(db)
        private = await add_work(db, catalog_public=False, catalog_owner_id=UUID(admin["id"]))
        seed_id, private_id = str(seed.id), str(private.id)
    await login_member(client, role="viewer")
    assert (await client.get(f"/api/discovery/related/{private_id}")).status_code == 404
    response = await client.get(f"/api/discovery/related/{seed_id}")
    assert response.status_code == 200 and response.json()["items"] == []
    assert not provider["calls"]


async def test_related_rechecks_seed_access_after_provider_io(client, admin, database, provider):
    async with database() as db, db.begin():
        seed = await add_work(db, "42", catalog_public=False, catalog_owner_id=UUID(admin["id"]))
        library = await add_owned(db, seed)
        seed_id, library_id = seed.id, library.id
    uid = await login_member(client)
    await connect(client)
    async with database() as db, db.begin():
        db.add(LibraryGrant(library_id=library_id, user_id=uid))

    async def revoke():
        async with database() as db, db.begin():
            await db.execute(delete(LibraryGrant))

    provider["hook"] = revoke
    response = await client.get(f"/api/discovery/related/{seed_id}")
    assert response.status_code == 404, response.text


async def test_discovery_requires_session_and_rejects_unbounded_or_unknown_queries(
    client, provider
):
    assert (await client.get("/api/discovery/local")).status_code == 401


async def test_discovery_input_bounds(client, admin):
    for path in [
        "/api/discovery/hardcover/other",
        "/api/discovery/hardcover/trending?page=26",
        "/api/discovery/hardcover/trending?page=0",
    ]:
        assert (await client.get(path)).status_code == 422
