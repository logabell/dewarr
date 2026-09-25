from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from app.adapters.catalog_providers import HC_BOOK
from app.db.models import (
    CatalogAccount,
    ProviderCache,
    User,
    Version,
    Work,
)
from app.domain.catalog_network import CatalogGateway
from app.security import hash_password

pytestmark = pytest.mark.integration


@pytest.fixture
def provider(monkeypatch):
    from app.api import metadata

    state = {
        "title": "A Catalog Book",
        "description": "First description",
        "narrator": "Reader One",
        "calls": [],
        "broken": False,
    }

    def respond(request):
        import json

        state["calls"].append(
            (request.method, request.url.path, request.headers.get("Authorization"))
        )
        if state["broken"]:
            return httpx.Response(200, json={"data": None, "errors": [{"message": "secret"}]})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL1W",
                            "title": "Fallback Book",
                            "author_name": ["Fallback Writer"],
                        }
                    ],
                    "numFound": 1,
                },
            )
        query = json.loads(request.content)["query"]
        if query == HC_BOOK:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "books": [
                            {
                                "id": 42,
                                "title": state["title"],
                                "description": state["description"],
                                "cached_contributors": [{"author": {"name": "Writer"}}],
                                "cached_image": {"url": "https://assets.hardcover.app/book.jpg"},
                            }
                        ],
                        "editions": [
                            {
                                "id": 70,
                                "book_id": 42,
                                "title": "A Catalog Book",
                                "reading_format": {"format": "Audio"},
                                "cached_contributors": [
                                    {
                                        "contribution": "Narrator",
                                        "author": {"name": state["narrator"]},
                                    }
                                ],
                            },
                            {
                                "id": 71,
                                "book_id": 42,
                                "title": "A Catalog Book",
                                "reading_format": {"format": "Physical"},
                            },
                        ],
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "search": {
                        "results": {
                            "hits": [
                                {
                                    "document": {
                                        "id": 42,
                                        "title": state["title"],
                                        "author_names": ["Writer"],
                                    }
                                }
                            ],
                            "found": 1,
                        }
                    }
                }
            },
        )

    class FakeGateway(CatalogGateway):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(respond))

        async def reserve(self):
            pass

    monkeypatch.setattr(metadata, "CatalogGateway", FakeGateway)
    return state


@pytest.mark.parametrize("empty", [False, True])
async def test_loading_lists_verifies_saved_connection(client, admin, database, monkeypatch, empty):
    from app.adapters.catalog_providers import Hardcover
    from app.adapters.contracts import AdapterError, FailureKind
    from app.adapters.hardcover_lists import ChoicePage, ListChoice

    broken = False

    async def choices(self, mode, cursor):
        if broken:
            raise AdapterError(FailureKind.AUTHENTICATION, "Invalid token")
        return ChoicePage(
            items=[]
            if empty
            else [
                ListChoice(external_id="42", name="My books", count=12, public=False, owner_id="7")
            ]
        )

    monkeypatch.setattr(Hardcover, "list_choices", choices)
    await connect(client)
    assert (await client.get("/api/metadata/account")).json()["status"] == "untested"
    response = await client.get("/api/metadata/hardcover-lists")
    assert response.status_code == 200, response.text
    account = (await client.get("/api/metadata/account")).json()
    assert account["status"] == "connected"
    assert account["last_success_at"]
    assert account["last_error"] is None

    await client.put("/api/metadata/account", json={"token": "replacement-token"})
    broken = True
    response = await client.get("/api/metadata/hardcover-lists")
    assert response.status_code == 409, response.text
    account = (await client.get("/api/metadata/account")).json()
    assert account["status"] == "authentication"
    assert account["last_error"] == "Invalid token"
    assert account["last_success_at"] is None


async def connect(client):
    response = await client.put("/api/metadata/account", json={"token": "hc-private-test-token"})
    assert response.status_code == 200, response.text
    assert "hc-private" not in response.text


async def test_verified_connection_survives_unchanged_saves(client, admin, database, provider):
    await connect(client)
    tested = (await client.post("/api/metadata/account/test")).json()
    assert tested["status"] == "connected"
    assert tested["last_success_at"]

    for payload in ({"enabled": True}, {"token": "hc-private-test-token", "enabled": True}):
        saved = await client.put("/api/metadata/account", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "connected"
        assert datetime.fromisoformat(saved.json()["last_success_at"]) == datetime.fromisoformat(
            tested["last_success_at"]
        )
        reloaded = (await client.get("/api/metadata/account")).json()
        assert reloaded == saved.json()

    changed = await client.put("/api/metadata/account", json={"token": "replacement-token"})
    assert changed.json()["status"] == "untested"
    assert changed.json()["last_success_at"] is None

    await client.post("/api/metadata/account/test")
    disabled = await client.put("/api/metadata/account", json={"enabled": False})
    assert disabled.json()["status"] == "disabled"
    assert not disabled.json()["enabled"]
    enabled = await client.put("/api/metadata/account", json={"enabled": True})
    assert enabled.json()["status"] == "untested"
    assert enabled.json()["last_success_at"] is None


async def test_catalog_import_provenance_locks_and_version_identity(
    client, admin, database, provider
):
    await connect(client)
    tested = await client.post("/api/metadata/account/test")
    assert tested.json()["status"] == "connected", tested.text
    searched = await client.get("/api/metadata/search", params={"q": "Book"})
    assert searched.json()["provider"] == "hardcover", searched.text
    assert searched.json()["items"][0]["title"] == "A Catalog Book"
    imported = await client.post("/api/metadata/books/hardcover/42/import")
    assert imported.status_code == 200, imported.text
    work = imported.json()
    assert not work["availability"]["owned"]
    repeated = await client.post("/api/metadata/books/hardcover/42/import")
    assert repeated.json()["id"] == work["id"]
    endpoint = f"/api/metadata/works/{work['id']}"
    metadata = await client.get(endpoint)
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["versions_total"] == 2
    assert metadata.json()["fields"]["title"]["provider"] == "hardcover"
    edited = await client.patch(
        endpoint, json={"values": {"title": "My chosen title", "description": None}}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["description"] is None
    provider["description"] = "Fresh provider description"
    provider["narrator"] = "Different Reader"
    refreshed = await client.post(
        endpoint + "/source", json={"provider": "hardcover", "external_id": "42"}
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["title"] == "My chosen title"
    assert refreshed.json()["description"] is None
    metadata = (await client.get(endpoint)).json()
    audio = next(v for v in metadata["versions"] if v["medium"] == "audio")
    assert audio["narrators"] == ["Reader One"]
    assert audio["needs_review"]
    assert not audio["owned"]
    unlocked = await client.patch(endpoint, json={"unlock": ["title", "description"]})
    assert unlocked.json()["description"] == "Fresh provider description"
    assert unlocked.json()["title"] == "A Catalog Book"
    provider["title"] = "Different Work"
    rejected = await client.post(
        endpoint + "/source", json={"provider": "hardcover", "external_id": "42"}
    )
    assert rejected.status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Work)) == 1
        assert await db.scalar(select(func.count()).select_from(Version)) == 2
        account = await db.get(CatalogAccount, UUID(admin["id"]))
        assert "hc-private" not in account.encrypted_token


async def test_catalog_private_tokens_fallback_and_protected_manual_entry(
    client, admin, database, provider
):
    before = await client.get("/api/metadata/search", params={"q": "Book"})
    assert before.json()["provider"] == "openlibrary"
    await connect(client)
    local = (
        await client.post(
            "/api/catalog/works",
            json={
                "title": "A Catalog Book",
                "authors": ["Writer"],
                "description": "My description",
            },
        )
    ).json()
    imported = await client.post("/api/metadata/books/hardcover/42/import")
    assert imported.json()["id"] == local["id"]
    assert imported.json()["description"] == "My description"
    provider["broken"] = True
    failure = await client.get(
        "/api/metadata/search", params={"q": "Different", "provider": "hardcover"}
    )
    assert failure.status_code == 502, failure.text
    assert "secret" not in failure.text
    # Create a separate account: it must neither see nor use the administrator's token.
    async with database() as db:
        member = User(
            username="reader",
            display_name="Reader",
            role="viewer",
            password_hash=hash_password("a long test password"),
        )
        db.add(member)
        await db.commit()
    await client.post("/api/auth/logout")
    login = await client.post(
        "/api/auth/login", json={"username": "reader", "password": "a long test password"}
    )
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    own = await client.get("/api/metadata/account")
    assert not own.json()["configured"]
    assert (
        await client.get("/api/metadata/search", params={"q": "Book", "provider": "hardcover"})
    ).status_code == 409
    assert (await client.post("/api/metadata/books/openlibrary/OL1W/import")).status_code == 403
    assert (
        await client.patch(
            f"/api/metadata/works/{local['id']}", json={"values": {"title": "wrong"}}
        )
    ).status_code == 403


async def test_persisted_cache_budget_and_error_responses(database):
    calls = []
    state = {"fail": False}

    def respond(request):
        calls.append(request)
        return httpx.Response(503 if state["fail"] else 200, json={"ok": True})

    async with CatalogGateway(
        "openlibrary", "public", transport=httpx.MockTransport(respond)
    ) as gateway:
        assert await gateway.request("GET", "test") == {"ok": True}
        assert await gateway.request("GET", "test") == {"ok": True}
        assert len(calls) == 1
        async with database() as db:
            row = await db.get(ProviderCache, gateway.used_keys[0])
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        state["fail"] = True
        assert await gateway.request("GET", "test") == {"ok": True}
        assert gateway.stale
    async with (
        CatalogGateway("hardcover", "user-a", "same-token") as a,
        CatalogGateway("hardcover", "user-b", "same-token") as b,
    ):
        assert a.budget_key == b.budget_key
        await a.cooldown(80)  # No existing reservation; cooldown cannot create a phantom budget.
        await a.reserve()
        await b.cooldown(80)
        from app.adapters.contracts import AdapterError, FailureKind

        with pytest.raises(AdapterError) as caught:
            await a.reserve()
        assert caught.value.kind == FailureKind.RATE_LIMIT
    async with CatalogGateway(
        "hardcover",
        "errors",
        "bad-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": None})),
    ) as gateway:
        await gateway.request("POST", "v1/graphql")
        async with database() as db:
            assert await db.get(ProviderCache, gateway.used_keys[0]) is None


async def test_concurrent_imports_share_identity(client, admin, database, provider):
    import asyncio

    await connect(client)
    responses = await asyncio.gather(
        *[client.post("/api/metadata/books/hardcover/42/import") for _ in range(5)]
    )
    assert all(response.status_code == 200 for response in responses), [
        response.text for response in responses
    ]
    assert len({response.json()["id"] for response in responses}) == 1
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Work)) == 1
        assert await db.scalar(select(func.count()).select_from(Version)) == 2


async def test_public_work_does_not_expose_ungranted_inventory_versions(
    client, admin, database, provider
):
    from app.db.models import AssetContains, Integration, Library, LibraryAsset

    await connect(client)
    work = (await client.post("/api/metadata/books/hardcover/42/import")).json()
    work_id = UUID(work["id"])
    async with database() as db:
        connection = Integration(
            kind="audiobookshelf",
            name="Private",
            base_url="http://private-abs",
            config={},
            encrypted_secrets="not-used",
        )
        reader = User(
            username="scoped",
            display_name="Scoped",
            role="viewer",
            password_hash=hash_password("a long test password"),
        )
        db.add_all([connection, reader])
        await db.flush()
        library = Library(integration_id=connection.id, external_id="private", name="Private books")
        version = Version(
            work_id=work_id, medium="audio", narrators=["Private Narrator"], identifiers={}
        )
        db.add_all([library, version])
        await db.flush()
        asset = LibraryAsset(
            library_id=library.id,
            external_id="secret",
            version_id=version.id,
            medium="audio",
            state="present",
            full_content=True,
            files=[],
        )
        db.add(asset)
        await db.flush()
        db.add(AssetContains(work_id=work_id, asset_id=asset.id, verified=True))
        await db.commit()
    endpoint = f"/api/metadata/works/{work_id}"
    assert (await client.get(endpoint)).json()["versions_total"] == 3
    await client.post("/api/auth/logout")
    login = await client.post(
        "/api/auth/login", json={"username": "scoped", "password": "a long test password"}
    )
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    result = await client.get(endpoint)
    assert result.status_code == 200, result.text
    assert result.json()["versions_total"] == 2
    assert "Private Narrator" not in result.text
    assert not any(version["owned"] for version in result.json()["versions"])
    catalog = (await client.get(f"/api/catalog/works/{work_id}")).json()
    assert not catalog["availability"]["owned"]


async def test_edition_pages_accumulate_and_repeat_import_does_not_reset_cursor(
    client, admin, database, provider, monkeypatch
):
    from app.adapters.catalog_providers import Hardcover
    from app.adapters.catalog_types import BookData, EditionData

    async def fetch(self, external_id, edition_offset=0):
        return BookData(
            provider="hardcover",
            external_id=external_id,
            title="Paged Book",
            authors=["Writer"],
            editions=[
                EditionData(external_id=str(index), medium="ebook", language="en")
                for index in range(
                    edition_offset, edition_offset + (50 if edition_offset == 0 else 3)
                )
            ],
            editions_more=edition_offset == 0,
            editions_offset=edition_offset,
        )

    monkeypatch.setattr(Hardcover, "fetch", fetch)
    await connect(client)
    work = (await client.post("/api/metadata/books/hardcover/42/import")).json()
    endpoint = f"/api/metadata/works/{work['id']}"
    metadata = (await client.get(endpoint)).json()
    assert metadata["versions_total"] == 50
    assert metadata["sources"][0]["editions_more"]
    response = await client.post(
        endpoint + "/source/editions", json={"provider": "hardcover", "external_id": "42"}
    )
    assert response.status_code == 200, response.text
    metadata = (await client.get(endpoint, params={"offset": 40})).json()
    assert metadata["versions_total"] == 53
    assert len(metadata["versions"]) == 13
    assert not metadata["sources"][0]["editions_more"]
    repeated = await client.post("/api/metadata/books/hardcover/42/import")
    assert repeated.json()["id"] == work["id"]
    assert not (await client.get(endpoint)).json()["sources"][0]["editions_more"]
    assert (
        await client.post(
            endpoint + "/source/editions", json={"provider": "hardcover", "external_id": "42"}
        )
    ).status_code == 409


async def test_changed_credentials_fence_inflight_provider_response(
    client, admin, provider, monkeypatch
):
    import asyncio

    from app.adapters.catalog_providers import Hardcover
    from app.adapters.catalog_types import SearchPage

    started, release = asyncio.Event(), asyncio.Event()

    async def search(self, query, page, filters=None):
        started.set()
        await release.wait()
        return SearchPage(provider="hardcover", items=[], page=page, has_more=False)

    monkeypatch.setattr(Hardcover, "search", search)
    await connect(client)
    pending = asyncio.create_task(
        client.get("/api/metadata/search", params={"q": "Book", "provider": "hardcover"})
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    try:
        response = await client.put("/api/metadata/account", json={"token": "rotated-test-token"})
        assert response.status_code == 200
    finally:
        release.set()
    assert (await pending).status_code == 409


async def test_field_preferences_and_cover_locks_only_use_accepted_sources(
    client, admin, provider, monkeypatch
):
    from app.adapters.catalog_providers import OpenLibrary
    from app.adapters.catalog_types import BookData

    async def fetch(self, external_id, edition_offset=0):
        return BookData(
            provider="openlibrary",
            external_id=external_id,
            title="A Catalog Book",
            authors=["Writer"],
            description="Open Library description",
            cover_url="https://covers.openlibrary.org/b/id/1-L.jpg",
        )

    monkeypatch.setattr(OpenLibrary, "fetch", fetch)
    await connect(client)
    work = (await client.post("/api/metadata/books/hardcover/42/import")).json()
    endpoint = f"/api/metadata/works/{work['id']}"
    matched = await client.post(
        endpoint + "/source",
        json={"provider": "openlibrary", "external_id": "OL1W", "confirm_match": True},
    )
    assert matched.status_code == 200, matched.text
    assert matched.json()["description"] == "First description"
    preference = await client.put(
        "/api/metadata/preferences", json={"field_providers": {"description": "openlibrary"}}
    )
    assert preference.status_code == 200
    refreshed = await client.post(
        endpoint + "/source", json={"provider": "openlibrary", "external_id": "OL1W"}
    )
    assert refreshed.json()["description"] == "Open Library description"
    cover = "https://covers.openlibrary.org/b/id/1-L.jpg"
    selected = await client.patch(endpoint, json={"values": {"cover_url": cover}})
    assert selected.status_code == 200, selected.text
    refreshed = await client.post(
        endpoint + "/source", json={"provider": "hardcover", "external_id": "42"}
    )
    assert refreshed.json()["cover_url"] == cover
    forbidden = await client.patch(
        endpoint, json={"values": {"cover_url": "https://example.com/unmatched.jpg"}}
    )
    assert forbidden.status_code == 422
    fields = (await client.get(endpoint)).json()["fields"]
    assert fields["description"]["provider"] == "openlibrary"
    assert fields["cover_url"]["locked"]


async def test_catalog_reader_exposes_accepted_snapshot_without_overwriting_local_fields(
    client, admin, database, provider
):
    from app.db.models import WorkMetadataSource

    await connect(client)
    imported = await client.post("/api/metadata/books/hardcover/42/import")
    assert imported.status_code == 200
    work_id = UUID(imported.json()["id"])
    async with database() as db:
        work = await db.get(Work, work_id)
        work.description = None
        await db.commit()
    response = await client.get(f"/api/metadata/works/{work_id}")
    assert response.status_code == 200
    source = response.json()["sources"][0]
    assert source["book"]["description"] == "First description"
    assert source["book"]["external_id"] == "42"
    async with database() as db:
        assert (await db.get(Work, work_id)).description is None
        saved_source = await db.get(WorkMetadataSource, UUID(source["id"]))
        saved_source.accepted = False
        await db.commit()
    assert (await client.get(f"/api/metadata/works/{work_id}")).json()["sources"] == []
