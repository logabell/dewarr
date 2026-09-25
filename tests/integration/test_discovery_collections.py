from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.db.models import DiscoveryFollow
from app.domain.discovery_catalog import refresh

pytestmark = pytest.mark.integration


@pytest.fixture
def sample(monkeypatch):
    value = {
        "id": "gr-list-50",
        "kind": "listopia",
        "title": "Epic fantasy",
        "source_url": "https://www.goodreads.com/list/show/50",
        "year": None,
        "category": None,
        "genres": ["fantasy"],
        "count": 1000,
        "coverage": "partial",
        "updated_at": datetime.now(UTC).isoformat(),
        "books": [
            {
                "external_id": str(i),
                "title": f"Book {i}",
                "authors": ["An Author"],
                "cover_url": None,
                "rank": i,
                "winner": i == 1,
            }
            for i in range(1, 55)
        ],
    }
    monkeypatch.setattr(
        "app.api.discovery_collections.catalog", lambda: {value["id"]: deepcopy(value)}
    )
    return value


async def test_browse_filters_before_pagination_and_layout_persists(client, admin, sample):
    response = await client.get("/api/discovery/collections?genre=fantasy")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["count"] == 1000
    detail = (await client.get("/api/discovery/collections/gr-list-50?page=2")).json()
    assert detail["total"] == 54 and len(detail["items"]) == 14
    assert detail["items"][0]["title"] == "Book 41"
    filtered = (await client.get("/api/discovery/browse?q=Book%205")).json()
    assert filtered["total"] == 6
    response = await client.put(
        "/api/discovery/layout", json={"hidden": ["library"], "order": ["personal", "library"]}
    )
    assert response.status_code == 200
    assert (await client.get("/api/discovery/layout")).json()["hidden"] == ["library"]


async def test_pin_and_tracking_are_independent_and_duplicate_add_reuses(
    client, admin, sample, database
):
    for _ in range(2):
        response = await client.post(
            "/api/discovery/collections",
            json={"url": sample["source_url"], "pinned": True, "tracking": True},
        )
        assert response.status_code == 200, response.text
    response = await client.put(
        "/api/discovery/collections/gr-list-50/follow", json={"pinned": False, "tracking": True}
    )
    assert response.status_code == 200
    assert response.json()["tracking"] and not response.json()["pinned"]
    async with database() as db:
        row = await db.get(DiscoveryFollow, (admin["id"], sample["id"]))
        assert row.generation == 3
    assert len((await client.get("/api/discovery/collections?saved=true")).json()["items"]) == 1


async def test_failed_or_obsolete_refresh_preserves_snapshot(
    client, admin, sample, database, monkeypatch
):
    await client.post(
        "/api/discovery/collections",
        json={"url": sample["source_url"], "pinned": True, "tracking": True},
    )

    async def failure(url):
        raise AdapterError(FailureKind.PARSER, "challenge")

    monkeypatch.setattr("app.domain.discovery_catalog.fetch_collection", failure)
    await refresh(admin["id"], sample["id"], 1)
    async with database() as db:
        row = await db.get(DiscoveryFollow, (admin["id"], sample["id"]))
        assert row.snapshot["books"] == sample["books"] and row.error
    await client.put(
        "/api/discovery/collections/gr-list-50/follow", json={"pinned": True, "tracking": False}
    )

    async def unexpected(url):
        raise AssertionError("Paused generations must not fetch")

    monkeypatch.setattr("app.domain.discovery_catalog.fetch_collection", unexpected)
    await refresh(admin["id"], sample["id"], 1)


async def test_another_reader_cannot_see_manual_private_selection(client, admin, sample):
    await client.post(
        "/api/discovery/collections",
        json={"url": sample["source_url"], "pinned": True, "tracking": True},
    )
    response = await client.post(
        "/api/auth/users",
        json={
            "username": "other-reader",
            "display_name": "Other",
            "password": "another long password",
            "role": "member",
        },
    )
    assert response.status_code in {200, 201}, response.text
    await client.post("/api/auth/logout")
    response = await client.post(
        "/api/auth/login", json={"username": "other-reader", "password": "another long password"}
    )
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert (await client.get("/api/discovery/collections?saved=true")).json()["items"] == []
    assert (await client.get("/api/discovery/layout")).json()["hidden"] == []


async def test_pinning_a_hidden_collection_restores_its_home_row(client, admin, sample):
    await client.put("/api/discovery/layout", json={"hidden": [sample["id"], "library"]})
    response = await client.post("/api/discovery/collections", json={"url": sample["source_url"]})
    assert response.status_code == 200
    assert (await client.get("/api/discovery/layout")).json()["hidden"] == ["library"]


@pytest.mark.parametrize("case", ["match", "wrong-author", "ambiguous", "disabled"])
async def test_goodreads_resolves_verified_hardcover_without_importing(
    client, admin, sample, monkeypatch, database, case
):
    from sqlalchemy import func, select

    from app.adapters.catalog_types import BookData, SearchPage
    from app.db.models import WorkMetadataSource

    if case != "disabled":
        response = await client.put(
            "/api/metadata/account", json={"token": "test-token", "enabled": True}
        )
        assert response.status_code == 200
    candidate = BookData(
        provider="hardcover",
        external_id="42",
        title="Book 1",
        authors=["An Author"],
        cover_url="https://assets.hardcover.app/large.jpg",
        description="Full details",
    )
    calls = []

    async def call(db, user_id, provider, operation, *args):
        calls.append(operation)
        await db.rollback()
        if operation in {"search", "title_search"}:
            choices = [candidate]
            if case == "wrong-author":
                choices = [candidate.model_copy(update={"authors": ["Different Author"]})]
            if case == "ambiguous":
                choices.append(candidate.model_copy(update={"external_id": "43"}))
            return (
                SearchPage(provider="hardcover", items=choices, page=1, has_more=False),
                False,
                None,
            )
        if operation == "fetch_many":
            books = {key: candidate.model_copy(update={"external_id": key}) for key in args[0]}
            return books, False, None
        return candidate.model_copy(update={"external_id": args[0]}), False, None

    monkeypatch.setattr("app.api.metadata.provider_call", call)
    response = await client.get("/api/discovery/goodreads/1")
    assert response.status_code == 200, response.text
    value = response.json()
    if case == "match":
        assert value["match"]["book"]["external_id"] == "42"
        assert value["match"]["book"]["cover_url"].endswith("large.jpg")
        assert calls == ["search", "fetch"]
    else:
        assert value["match"]["book"] is None
    if case == "disabled":
        assert not calls and value["match"]["status"] == "disabled"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(WorkMetadataSource)) == 0
    assert (await client.get("/api/discovery/goodreads/not-in-catalog")).status_code == 404


@pytest.fixture
def long_collection(sample, monkeypatch):
    sample["books"] = [
        {**sample["books"][0], "external_id": str(n), "title": f"Book {n}", "rank": n}
        for n in range(1, 101)
    ]
    sample["count"] = 201
    calls = []

    async def fetch_page(url, page):
        calls.append(page)
        return {
            "books": [
                {**sample["books"][0], "external_id": str(n), "title": f"Book {n}", "rank": n}
                for n in range((page - 1) * 100 + 1, min(page * 100, 201) + 1)
            ],
            "count": 201,
            "has_more": page < 3,
        }

    monkeypatch.setattr("app.domain.discovery_pages.fetch_collection_page", fetch_page)
    return sample, calls


async def test_full_collection_crosses_source_boundaries_and_caches(client, admin, long_collection):
    sample, calls = long_collection
    url = f"/api/discovery/collections/{sample['id']}"
    preview = (await client.get(url + "?page=3")).json()
    assert len(preview["items"]) == 20 and not preview["has_more"]
    assert preview["total"] == 100 and calls == []
    ids = []
    for page in range(1, 7):
        response = await client.get(url + f"?full=true&page={page}")
        assert response.status_code == 200, response.text
        data = response.json()
        ids.extend(b["external_id"] for b in data["items"])
        assert data["total"] == 201
        assert data["has_more"] == (page < 6)
    assert ids == [str(n) for n in range(1, 202)]
    assert calls == [1, 2, 3]
    again = await client.get(url + "?full=true&page=3")
    assert len(again.json()["items"]) == 40 and calls == [1, 2, 3]
    # Newly loaded books open natively, even without a Hardcover connection.
    book = await client.get("/api/discovery/goodreads/101")
    assert book.status_code == 200, book.text
    assert book.json()["entry"]["title"] == "Book 101"
    # Expansion never changes the home shelf snapshot or follows a collection.
    preview = (await client.get(url + "?page=3")).json()
    assert len(preview["items"]) == 20 and not preview["has_more"]
    assert (await client.get("/api/discovery/collections?saved=true")).json()["items"] == []


async def test_failed_page_is_retryable_without_losing_cached_books(
    client,
    admin,
    long_collection,
    monkeypatch,
):
    sample, calls = long_collection
    url = f"/api/discovery/collections/{sample['id']}"
    await client.get(url + "?full=true&page=3")

    async def fail(*args):
        raise AdapterError(FailureKind.UNAVAILABLE, "try later")

    monkeypatch.setattr("app.domain.discovery_pages.fetch_collection_page", fail)
    response = await client.get(url + "?full=true&page=6")
    assert response.status_code == 502 and "Try again" in response.json()["detail"]
    cached = await client.get(url + "?full=true&page=4")
    assert cached.status_code == 200 and cached.json()["items"][0]["external_id"] == "121"
    assert (await client.get("/api/discovery/goodreads/101")).status_code == 200


async def test_cached_expansion_is_reader_scoped_and_invalidated_by_snapshot_change(
    client,
    admin,
    long_collection,
):
    sample, calls = long_collection
    await client.get(f"/api/discovery/collections/{sample['id']}?full=true&page=3")
    assert (await client.get("/api/discovery/goodreads/101")).status_code == 200
    old = sample["updated_at"]
    sample["updated_at"] = datetime.now(UTC).isoformat()
    assert (await client.get("/api/discovery/goodreads/101")).status_code == 404
    sample["updated_at"] = old
    response = await client.post(
        "/api/auth/users",
        json={
            "username": "another",
            "display_name": "Another",
            "password": "a long test password",
            "role": "member",
        },
    )
    assert response.status_code in {200, 201}
    await client.post("/api/auth/logout")
    response = await client.post(
        "/api/auth/login",
        json={
            "username": "another",
            "password": "a long test password",
        },
    )
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert (await client.get("/api/discovery/goodreads/101")).status_code == 404
    await client.get(f"/api/discovery/collections/{sample['id']}?full=true&page=3")
    assert calls == [1, 2, 1, 2]
    assert (await client.get("/api/discovery/goodreads/101")).status_code == 200


async def test_growing_source_count_does_not_stop_at_original_snapshot_total(
    client,
    admin,
    long_collection,
):
    sample, calls = long_collection
    sample["count"] = 180
    url = f"/api/discovery/collections/{sample['id']}?full=true"
    page = (await client.get(url + "&page=3")).json()
    assert page["total"] == 201
    last = (await client.get(url + "&page=6")).json()
    assert last["items"][0]["external_id"] == "201" and not last["has_more"]
    assert calls == [1, 2, 3]


async def test_expired_count_does_not_hide_new_pages(client, admin, database, long_collection):
    from datetime import timedelta
    from uuid import UUID

    from app.db.models import ProviderCache
    from app.domain.discovery_pages import collection_key

    sample, calls = long_collection
    url = f"/api/discovery/collections/{sample['id']}?full=true"
    await client.get(url + "&page=1")
    async with database() as db, db.begin():
        count = await db.get(ProviderCache, collection_key(UUID(admin["id"]), sample, "count"))
        count.value = {"count": 1}
        count.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    response = await client.get(url + "&page=6")
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["external_id"] == "201"
    assert calls == [1, 3]
