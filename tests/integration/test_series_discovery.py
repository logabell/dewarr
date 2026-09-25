from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update

from app.adapters.catalog_types import BookData
from app.db.models import (
    AcquisitionIntent,
    AssetContains,
    CatalogSeries,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    Operation,
    SeriesMembership,
    Work,
)
from tests.integration.test_catalog_series import record
from tests.integration.test_discovery import add_owned, login_member

pytestmark = pytest.mark.integration


async def add_book(db, title, **values):
    work = Work(title=title, authors=["Coastal Writer"], **values)
    db.add(work)
    await db.flush()
    return work


async def add_series(db, owner, members, external_id="9", **values):
    series = CatalogSeries(
        owner_id=UUID(str(owner)),
        provider="hardcover",
        external_id=external_id,
        name=f"Coast {external_id}",
        fetched_at=values.pop("fetched_at", datetime.now(UTC)),
        **values,
    )
    db.add(series)
    await db.flush()
    for index, (work, fields) in enumerate(members, 1):
        db.add(
            SeriesMembership(
                series_id=series.id,
                work_id=work.id,
                external_id=str(index),
                snapshot={
                    **record(index),
                    "book": BookData(
                        provider="hardcover",
                        external_id=str(index),
                        title=work.title,
                        authors=work.authors,
                    ).model_dump(mode="json"),
                    **{k: v for k, v in fields.items() if k != "present"},
                },
                present=fields.get("present", True),
            )
        )
    await db.flush()
    return series


async def get_shelf(client, **params):
    response = await client.get("/api/discovery/series", params=params)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    return response.json()


async def test_published_gaps_have_medium_specific_ownership_without_browse_effects(
    client, admin, database
):
    async with database() as db, db.begin():
        first = await add_book(db, "Book one")
        second = await add_book(db, "Book two")
        third = await add_book(db, "Book three")
        await add_owned(db, first)
        audio_library = await add_owned(db, third)
        await db.execute(
            update(LibraryAsset)
            .where(LibraryAsset.library_id == audio_library.id)
            .values(medium="audio")
        )
        await add_series(db, admin["id"], [(w, {}) for w in (first, second, third)])
        ids = [str(w.id) for w in (first, second, third)]
    shelf = await get_shelf(client)
    item = shelf["items"][0]
    assert item["owned"] == 2
    assert item["ebook"] == item["audio"] == item["missing"] == 1
    assert item["published"] == 3
    assert [book["work"]["id"] for book in item["books"]] == [ids[1]]
    assert item["catalog_stale"] is item["inventory_stale"] is False
    audio = (await get_shelf(client, medium="audio"))["items"][0]
    assert [book["work"]["id"] for book in audio["books"]] == ids[:2]
    assert audio["books"][0]["work"]["availability"]["owned"] is True
    assert audio["books"][0]["work"]["availability"]["audio"] is False
    ebook = (await get_shelf(client, medium="ebook"))["items"][0]
    assert [book["work"]["id"] for book in ebook["books"]] == ids[1:]
    async with database() as db:
        for model in (Operation, AcquisitionIntent):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        assert await db.scalar(select(func.count()).select_from(Work)) == 3


async def test_catalog_uncertainty_and_stale_holdings_are_explicit(client, admin, database):
    async with database() as db, db.begin():
        first = await add_book(db, "Owned")
        await add_owned(db, first)
        await db.execute(update(LibraryAsset).values(state="stale"))
        members = [(first, {})]
        for title, fields in [
            ("Ambiguous A", {"position": "2"}),
            ("Ambiguous B", {"position": "2.0"}),
            ("Unordered", {"position": None}),
            ("Unknown date", {"release_date": None}),
            ("Future", {"release_date": "2999-01-01"}),
            ("Collection", {"compilation": True}),
            ("Excerpt", {"partial": True}),
            ("Upstream merged", {"canonical_id": "123"}),
            ("No longer a member", {"present": False}),
        ]:
            members.append((await add_book(db, title), fields))
        await add_series(db, admin["id"], members, fetched_at=datetime.now(UTC) - timedelta(days=3))
    item = (await get_shelf(client))["items"][0]
    assert item["catalog_stale"] is item["inventory_stale"] is True
    assert item["owned"] == 1 and item["published"] == 4 and item["missing"] == 3
    assert item["future_publication"] == item["unknown_publication"] == 1
    assert [book["work"]["title"] for book in item["books"]] == [
        "Ambiguous A",
        "Ambiguous B",
        "Unordered",
    ]
    assert [book["ambiguous_position"] for book in item["books"]] == [True, True, False]
    assert item["books"][-1]["position"] is None


async def test_canonical_merges_group_gaps_and_owned_origins(client, admin, database):
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned original")
        owned_root = await add_book(db, "Owned canonical")
        gap = await add_book(db, "Gap original")
        gap_root = await add_book(db, "Gap canonical")
        await add_owned(db, owned)
        owned.redirect_to = owned_root.id
        gap.redirect_to = gap_root.id
        await add_series(db, admin["id"], [(w, {}) for w in (owned, owned_root, gap, gap_root)])
        root_id = str(gap_root.id)
    item = (await get_shelf(client))["items"][0]
    assert item["owned"] == item["missing"] == 1
    assert item["published"] == 2
    assert len(item["books"]) == 1
    assert item["books"][0]["work"]["id"] == root_id
    assert item["books"][0]["ambiguous_position"] is True


async def test_verified_collection_children_supply_ownership(client, admin, database):
    async with database() as db, db.begin():
        works = [await add_book(db, title) for title in ["First", "Second", "Third"]]
        library = await add_owned(db, works[0])
        asset = await db.scalar(select(LibraryAsset).where(LibraryAsset.library_id == library.id))
        asset.containment = {"kind": "reviewed"}
        db.add(AssetContains(asset_id=asset.id, work_id=works[1].id, verified=True))
        await add_series(db, admin["id"], [(work, {}) for work in works])
    item = (await get_shelf(client))["items"][0]
    assert item["owned"] == 2 and item["missing"] == 1
    audio = (await get_shelf(client, medium="audio"))["items"][0]
    assert audio["books"][0]["work"]["availability"]["in_collection"] is True


@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_catalogs_and_holdings_are_scoped_and_revocation_is_immediate(
    client, admin, database, role
):
    owner = await login_member(client, role)
    async with database() as db, db.begin():
        first = await add_book(db, "Owned")
        gap = await add_book(db, "Visible gap", catalog_public=False, catalog_owner_id=owner)
        private_gap = await add_book(
            db, "Another person's title", catalog_public=False, catalog_owner_id=UUID(admin["id"])
        )
        library = await add_owned(db, first)
        library_id = library.id
        await add_series(db, owner, [(first, {}), (gap, {}), (private_gap, {})])
        await add_series(db, admin["id"], [(first, {}), (gap, {})], external_id="999")
    assert (await get_shelf(client))["items"] == []
    async with database() as db, db.begin():
        db.add(LibraryGrant(user_id=owner, library_id=library_id))
    shelf = await get_shelf(client)
    assert [item["external_id"] for item in shelf["items"]] == ["9"]
    assert shelf["items"][0]["missing"] == 1
    assert shelf["items"][0]["books"][0]["work"]["title"] == "Visible gap"
    async with database() as db, db.begin():
        await db.execute(delete(LibraryGrant))
    assert (await get_shelf(client))["items"] == []


@pytest.mark.parametrize(
    "condition", ["unverified", "companion", "missing", "inaccessible", "disabled"]
)
async def test_unusable_holdings_do_not_seed_recommendations(client, admin, database, condition):
    async with database() as db, db.begin():
        first = await add_book(db, "Seed")
        gap = await add_book(db, "Gap")
        await add_owned(db, first)
        await add_series(db, admin["id"], [(first, {}), (gap, {})])
        model, values = {
            "unverified": (AssetContains, {"verified": False}),
            "companion": (LibraryAsset, {"full_content": False}),
            "missing": (LibraryAsset, {"state": "missing"}),
            "inaccessible": (Library, {"accessible": False}),
            "disabled": (Integration, {"enabled": False}),
        }[condition]
        await db.execute(update(model).values(**values))
    assert (await get_shelf(client))["items"] == []


async def test_eligible_series_are_paginated_after_completed_unowned_and_unpublished_exclusions(
    client, admin, database
):
    async with database() as db, db.begin():
        seed = await add_book(db, "Seed")
        gap = await add_book(db, "Gap")
        await add_owned(db, seed)
        observed = datetime.now(UTC)
        for index in range(6):
            await add_series(
                db,
                admin["id"],
                [(seed, {}), (gap, {})],
                external_id=str(10 + index),
                fetched_at=observed - timedelta(minutes=index),
            )
        await add_series(db, admin["id"], [(seed, {})], external_id="100")
        await add_series(db, admin["id"], [(gap, {})], external_id="101")
        await add_series(
            db, admin["id"], [(seed, {}), (gap, {"release_date": None})], external_id="102"
        )
        await add_series(
            db, admin["id"], [(seed, {}), (gap, {"release_date": "2999-01-01"})], external_id="103"
        )
        await add_series(
            db, admin["id"], [(seed, {}), (gap, {})], external_id="104", fetched_at=None
        )
    first = await get_shelf(client)
    assert [item["external_id"] for item in first["items"]] == ["10", "11", "12", "13"]
    assert first["has_more"] is True
    second = await get_shelf(client, page=2)
    assert [item["external_id"] for item in second["items"]] == ["14", "15"]
    assert second["has_more"] is False
    assert (await get_shelf(client, page=3))["items"] == []
    single = await get_shelf(client, limit=1)
    assert len(single["items"]) == 1 and single["has_more"] is True


async def test_authentication_and_query_limits(client, admin):
    for params in ({"medium": "print"}, {"page": 0}, {"page": 101}, {"limit": 13}, {"limit": 0}):
        response = await client.get("/api/discovery/series", params=params)
        assert response.status_code == 422
    await client.post("/api/auth/logout")
    assert (await client.get("/api/discovery/series")).status_code == 401


async def test_multiple_large_series_use_batched_reads_and_bounded_cards(client, admin, database):
    from sqlalchemy import event

    from app.db.session import get_engine

    async with database() as db, db.begin():
        seed = await add_book(db, "Owned first book")
        await add_owned(db, seed)
        works = [Work(title=f"Missing book {index}", authors=["Writer"]) for index in range(250)]
        db.add_all(works)
        await db.flush()
        for index in range(5):
            await add_series(
                db,
                admin["id"],
                [(seed, {})] + [(work, {}) for work in works],
                external_id=str(index + 1),
            )
    statements = []

    def record_query(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record_query)
    try:
        await get_shelf(client, limit=1)
        single_count = len(statements)
        statements.clear()
        page = await get_shelf(client, limit=4)
        assert len(statements) == single_count
        assert len(page["items"]) == 4
        assert all(item["missing"] == 250 and len(item["books"]) == 3 for item in page["items"])
    finally:
        event.remove(engine, "before_cursor_execute", record_query)


async def test_title_lookalikes_do_not_seed_series_or_satisfy_missing_books(
    client, admin, database
):
    async with database() as db, db.begin():
        owned = await add_book(db, "Harbor")
        lookalike = await add_book(db, "Harbor: A Different Voyage")
        gap = await add_book(db, "Far Shore")
        await add_owned(db, owned)
        await add_series(db, admin["id"], [(lookalike, {}), (gap, {})])
        await add_series(db, admin["id"], [(owned, {}), (lookalike, {})], external_id="10")
        gap_id = str(lookalike.id)
    shelf = await get_shelf(client)
    assert [s["external_id"] for s in shelf["items"]] == ["10"]
    assert shelf["items"][0]["owned"] == 1
    assert shelf["items"][0]["books"][0]["work"]["id"] == gap_id
    detail = (await client.get("/api/catalog/series/hardcover/9")).json()
    assert detail["owned"] == 0
    assert all(not entry["work"]["availability"]["owned"] for entry in detail["items"])


async def test_saved_member_metadata_and_numeric_order_are_used_without_hydration(
    client, admin, database, monkeypatch
):
    from app.domain.catalog_network import CatalogGateway

    async def no_provider_calls(*args, **kwargs):
        raise AssertionError("Browsing saved series must not call Hardcover")

    monkeypatch.setattr(CatalogGateway, "request", no_provider_calls)
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        await add_owned(db, owned)
        members = [(owned, {})]
        for number in (10, 2, 1.5):
            work = await add_book(db, f"Outdated title {number}")
            book = BookData(
                provider="hardcover",
                external_id=str(len(members) + 10),
                title=f"Voyage {number}",
                authors=["Verified Writer"],
                cover_url=f"https://example.com/cover-{number}.jpg",
                publication_year=2020,
            ).model_dump(mode="json")
            members.append((work, {"book": book, "position": str(number)}))
        await add_series(db, admin["id"], members)
    for page in (1, 2):
        shelf = await get_shelf(client, page=page)
        if page == 1:
            books = shelf["items"][0]["books"]
            assert [b["position"] for b in books] == ["1.5", "2", "10"]
            assert [b["work"]["title"] for b in books] == ["Voyage 1.5", "Voyage 2", "Voyage 10"]
            assert books[0]["work"]["cover_url"] == "https://example.com/cover-1.5.jpg"
            assert books[0]["work"]["authors"] == ["Verified Writer"]
    detail = (await client.get("/api/catalog/series/hardcover/9")).json()
    assert detail["items"][1]["work"] == books[0]["work"]
