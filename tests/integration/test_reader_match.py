from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.adapters.catalog_types import BookData, SearchPage
from app.db.models import User, Work, WorkMetadataSource

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "case", ["match", "ambiguous", "wrong-author", "more", "changed", "rejected", "private"]
)
async def test_inventory_reader_lookup_is_conservative_and_read_only(
    client, admin, database, monkeypatch, case
):
    from app.api import metadata

    async with database() as db:
        actor = await db.get(User, UUID(admin["id"]))
        actor.role = "viewer"
        work = Work(
            title="The Giver of Stars", authors=["Jojo Moyes"], catalog_public=case != "private"
        )
        db.add(work)
        await db.flush()
        work_id = work.id
        if case == "rejected":
            db.add(
                WorkMetadataSource(
                    work_id=work_id,
                    provider="hardcover",
                    external_id="42",
                    accepted=False,
                    snapshot={},
                    fetched_at=datetime.now(UTC),
                )
            )
        await db.commit()
    calls = []
    candidate = BookData(
        provider="hardcover",
        external_id="42",
        title="The Giver of Stars",
        authors=["Jojo Moyes"],
        description="A story of friendship and books.",
    )

    async def provider_call(db, user_id, provider, operation, *args):
        calls.append(operation)
        await db.rollback()
        if operation in {"search", "title_search"}:
            items = [candidate]
            if case == "ambiguous":
                items.append(candidate.model_copy(update={"external_id": "43"}))
            if case == "wrong-author":
                items = [candidate.model_copy(update={"authors": ["Someone Else"]})]
            return (
                SearchPage(provider="hardcover", items=items, page=1, has_more=case == "more"),
                False,
                None,
            )
        if operation == "fetch_many":
            books = {key: candidate.model_copy(update={"external_id": key}) for key in args[0]}
            return books, False, None
        if case == "changed":
            return candidate.model_copy(update={"title": "A Different Book"}), False, None
        return candidate, False, None

    monkeypatch.setattr(metadata, "provider_call", provider_call)
    response = await client.get(f"/api/metadata/works/{work_id}/reader-match")
    if case == "private":
        assert response.status_code == 404
        assert not calls
        return
    assert response.status_code == 200, response.text
    if case == "match":
        assert response.json()["book"]["description"] == candidate.description
        assert response.json()["status"] == "matched"
        assert calls == ["search", "fetch"]
    else:
        assert response.json()["book"] is None
    if case == "rejected":
        assert not calls
    async with database() as db:
        work = await db.get(Work, work_id)
        assert work.description is None
        assert await db.scalar(select(func.count()).select_from(WorkMetadataSource)) == (
            case == "rejected"
        )


async def test_reader_lookup_requires_login(client):
    response = await client.get(f"/api/metadata/works/{UUID(int=42)}/reader-match")
    assert response.status_code == 401


async def test_save_verified_match_persists_source_without_changing_library_version(
    client, admin, database, monkeypatch
):
    from app.adapters.catalog_types import EditionData
    from app.api import metadata
    from app.db.models import LibraryAsset
    from tests.integration.test_library_discovery import add

    async with database() as db, db.begin():
        work, _, asset = await add(
            db,
            "The Giver of Stars (Unabridged)",
            metadata_snapshot={"identifiers": {"isbn": "0-306-40615-2"}},
        )
        work.authors = ["Jojo Moyes"]
        work.language = "English"
        work.metadata_fields = {"fields": {"title": {"locked": True}}}
        work_id, asset_id, version_id = work.id, asset.id, asset.version_id
    candidate = BookData(
        provider="hardcover",
        external_id="42",
        title="The Giver of Stars",
        authors=["Jojo Moyes"],
        description="Provider synopsis",
        editions=[
            EditionData(
                external_id="7",
                identifiers={"isbn_13": "9780306406157"},
                language="en",
                medium="print",
            )
        ],
    )

    async def call(db, user_id, provider, operation, *args):
        await db.rollback()
        if operation == "identifier_search":
            assert args[0] == [("isbn", "9780306406157")]
            return (
                SearchPage(provider="hardcover", items=[candidate], page=1, has_more=False),
                False,
                None,
            )
        assert operation == "fetch"
        return candidate, False, None

    monkeypatch.setattr(metadata, "provider_call", call)
    response = await client.post(f"/api/metadata/works/{work_id}/match-hardcover")
    assert response.status_code == 200, response.text
    assert response.json()["basis"] == "identifier"
    async with database() as db:
        source = await db.scalar(
            select(WorkMetadataSource).where(WorkMetadataSource.work_id == work_id)
        )
        assert source.accepted and source.external_id == "42" and not source.manual_match
        work = await db.get(Work, work_id)
        assert work.title.endswith("(Unabridged)")  # Manual metadata locks survive.
        assert work.description == "Provider synopsis"
        assert (await db.get(LibraryAsset, asset_id)).version_id == version_id
    again = await client.post(f"/api/metadata/works/{work_id}/match-hardcover")
    assert again.json()["status"] == "disabled"


async def test_save_match_rechecks_identity_after_provider_io(client, admin, database, monkeypatch):
    from app.api import metadata

    async with database() as db, db.begin():
        work = Work(title="Original", authors=["Writer"], catalog_public=True)
        db.add(work)
        await db.flush()
        work_id = work.id
    candidate = BookData(
        provider="hardcover", external_id="42", title="Original", authors=["Writer"]
    )

    async def call(db, user_id, provider, operation, *args):
        await db.rollback()
        if operation == "search":
            return (
                SearchPage(provider="hardcover", items=[candidate], page=1, has_more=False),
                False,
                None,
            )
        current = await db.get(Work, work_id)
        current.title = "Corrected during lookup"
        await db.commit()
        return candidate, False, None

    monkeypatch.setattr(metadata, "provider_call", call)
    response = await client.post(f"/api/metadata/works/{work_id}/match-hardcover")
    assert response.status_code == 200
    assert response.json()["status"] == "unmatched"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(WorkMetadataSource)) == 0


@pytest.mark.parametrize("changed", [False, True])
async def test_goodreads_shelf_isbns_supply_reader_metadata(
    client, admin, database, monkeypatch, changed
):
    from app.adapters.catalog_types import EditionData
    from app.db.models import BookList, ListSubscription
    from app.domain.list_subscriptions import apply_records
    from app.security import encrypt_secrets

    async with database() as db, db.begin():
        owner = await db.get(User, UUID(admin["id"]))
        shelf = BookList(owner_id=owner.id, name="Want to read", shared=False)
        db.add(shelf)
        await db.flush()
        subscription = ListSubscription(
            list_id=shelf.id, provider="goodreads", encrypted_config=encrypt_secrets({})
        )
        db.add(subscription)
        await db.flush()
        record = {
            "external_id": "123",
            "title": "Atmosphere",
            "authors": ["Taylor Jenkins Reid"],
            "isbn13": "9780306406157",
        }
        await apply_records(db, subscription, owner, [record])
        work = await db.scalar(select(Work).where(Work.title == "Atmosphere"))
        work_id, list_id = work.id, shelf.id
        if changed:
            await apply_records(db, subscription, owner, [{**record, "title": "Other book"}])
    candidate = BookData(
        provider="hardcover",
        external_id="42",
        title="Atmosphere",
        authors=["Taylor Jenkins Reid"],
        description="Hardcover synopsis",
        cover_url="https://assets.hardcover.app/atmosphere.jpg",
        editions=[EditionData(external_id="7", identifiers={"isbn_13": "9780306406157"})],
    )
    calls = []

    async def call(db, user_id, provider, operation, *args):
        calls.append(operation)
        await db.rollback()
        if operation == "identifier_search":
            assert args[0] == [("isbn", "9780306406157")]
            return (
                SearchPage(provider="hardcover", items=[candidate], page=1, has_more=False),
                False,
                None,
            )
        if operation == "search":
            return SearchPage(provider="hardcover", items=[], page=1, has_more=False), False, None
        assert operation == "fetch"
        return candidate, False, None

    monkeypatch.setattr("app.api.metadata.provider_call", call)
    response = await client.get(f"/api/metadata/works/{work_id}/reader-match")
    assert response.status_code == 200, response.text
    if changed:
        assert calls == ["search"]
        assert response.json()["book"] is None
    else:
        assert calls == ["identifier_search", "fetch"]
        assert response.json()["book"]["cover_url"] == candidate.cover_url
        assert response.json()["book"]["description"] == candidate.description
    detail = await client.get(f"/api/lists/{list_id}")
    assert detail.json()["items"][0]["id"] == str(work_id)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(WorkMetadataSource)) == 0
