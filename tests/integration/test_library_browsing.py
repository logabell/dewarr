from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, update

from app.db.models import (
    AcquisitionIntent,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    Operation,
    Work,
)
from tests.integration.test_discovery import add_owned, login_member
from tests.integration.test_library_discovery import NOW
from tests.integration.test_library_discovery import add as add_observed

pytestmark = pytest.mark.integration


async def add(db, work_title, **values):
    title = values.pop("title", work_title)
    work, library, asset = await add_observed(db, work_title, **values)
    asset.title = title
    return work, library, asset


async def browse(client, **params):
    response = await client.get("/api/library/assets", params=params)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    return response.json()


async def test_searches_library_titles_credits_and_corrected_catalog_without_payload_noise(
    client, admin, database
):
    async with database() as db, db.begin():
        root, _, asset = await add(
            db,
            "Original catalog title",
            medium="audio",
            title="Raw library title",
            metadata_snapshot={
                "authors": ["Library Author"],
                "narrators": ["Specific Narrator"],
                "description": "Not a searchable token",
            },
        )
        root.title = "Protected corrected title"
        root.authors = ["Catalog Author"]
        identity = str(asset.id)
        db.add(Work(title="Unowned unrelated catalog title", authors=[]))
    for query in [
        "raw LIBRARY",
        "library author",
        "Specific Narrator",
        "protected corrected",
        "catalog author",
    ]:
        result = await browse(client, q=query)
        assert result["total"] == 1
        assert result["items"][0]["id"] == identity
        assert result["items"][0]["authors"] == ["Library Author"]
    for query in ["Not a searchable token", "Unowned unrelated"]:
        assert (await browse(client, q=query))["total"] == 0
    async with database() as db:
        for model in (Operation, AcquisitionIntent):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_literal_wildcards_and_canonical_matches_do_not_duplicate_copies(
    client, admin, database
):
    async with database() as db, db.begin():
        origin, _, asset = await add(db, "Original", title="100%_complete\\book")
        root = Work(title="Canonical corrected", authors=["Canonical author"])
        db.add(root)
        await db.flush()
        origin.redirect_to = root.id
        _, _, other = await add(db, "Ordinary", title="100 percent complete book")
        identity = str(asset.id)
    for query in ["%", "_", "\\", "canonical corrected", "Canonical author"]:
        result = await browse(client, q=query)
        assert [item["id"] for item in result["items"]] == [identity]
        assert result["total"] == 1
    assert (await browse(client, q="  "))["total"] == 2


async def test_filters_count_and_sort_before_pagination_with_stable_observation_dates(
    client, admin, database
):
    async with database() as db, db.begin():
        _, lib, a = await add(db, "A", days=10, title="Alpha", medium="audio")
        _, _, b = await add(
            db,
            "B",
            days=1,
            title="Beta",
            medium="ebook",
            state="stale",
            match_status="needs-review",
        )
        _, _, c = await add(
            db, "C", days=5, title="Charlie", medium="audio", state="missing-confirmed"
        )
        ids = [str(a.id), str(b.id), str(c.id)]
        library_id = str(lib.id)
    all_items = await browse(client)
    assert [row["id"] for row in all_items["items"]] == ids
    recent = await browse(client, sort="recent", limit=1)
    assert recent["total"] == 3 and recent["items"][0]["id"] == ids[1]
    assert (await browse(client, sort="recent", limit=1, offset=1))["items"][0]["id"] == ids[2]
    assert (await browse(client, medium="audio", limit=1, offset=1))["items"][0]["id"] == ids[2]
    filtered = await browse(client, medium="audio", state="missing-confirmed", q="char")
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == ids[2]
    assert (await browse(client, medium="ebook", needs_review="true"))["items"][0]["id"] == ids[1]
    assert (await browse(client, library_id=library_id))["total"] == 1
    assert (await browse(client, medium="ebook", library_id=library_id))["total"] == 0
    assert (await browse(client, offset=100))["items"] == []
    async with database() as db, db.begin():
        await db.execute(update(LibraryAsset).values(last_seen_at=NOW + timedelta(days=1)))
    assert (await browse(client, sort="recent", limit=1))["items"][0]["id"] == ids[1]


@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_filters_never_expose_inaccessible_inventory_or_counts(client, admin, database, role):
    async with database() as db, db.begin():
        _, allowed, _ = await add(
            db,
            "Public work",
            title="Visible copy",
            metadata_snapshot={"authors": ["Shared author"]},
        )
        _, hidden, _ = await add(
            db,
            "Private work",
            title="Secret copy",
            metadata_snapshot={"authors": ["Shared author"]},
        )
        allowed_id, hidden_id, integration_id = allowed.id, hidden.id, allowed.integration_id
    owner = await login_member(client, role)
    assert (await browse(client, q="Shared author"))["total"] == 0
    async with database() as db, db.begin():
        db.add(LibraryGrant(library_id=allowed_id, user_id=owner))
    result = await browse(client, q="Shared author")
    assert result["total"] == 1 and result["items"][0]["title"] == "Visible copy"
    assert (await browse(client, library_id=str(hidden_id), q="Secret"))["total"] == 0
    async with database() as db, db.begin():
        await db.execute(update(Library).where(Library.id == allowed_id).values(accessible=False))
    assert (await browse(client, q="Shared author"))["total"] == 0
    async with database() as db, db.begin():
        await db.execute(update(Library).where(Library.id == allowed_id).values(accessible=True))
        await db.execute(
            update(Integration).where(Integration.id == integration_id).values(enabled=False)
        )
    assert (await browse(client, q="Shared author"))["total"] == 0
    async with database() as db, db.begin():
        await db.execute(
            update(Integration).where(Integration.id == integration_id).values(enabled=True)
        )
        await db.execute(delete(LibraryGrant))
    assert (await browse(client, q="Shared author"))["total"] == 0


@pytest.mark.parametrize(
    "params", [{"q": "x" * 301}, {"medium": "video"}, {"state": "unknown"}, {"sort": "random"}]
)
async def test_invalid_library_view_parameters(client, admin, params):
    assert (await client.get("/api/library/assets", params=params)).status_code == 422


async def test_file_locations_only_appear_for_accessible_library_copies(client, admin, database):
    async with database() as db, db.begin():
        work, library, asset = await add(db, "Files and locations")
        asset.files = [{"path": "/library/Author/Book/book.epub", "format": "epub", "size": 1234}]
        work_id = str(work.id)
    result = await browse(client, work_id=work_id)
    assert result["items"][0]["files"] == [
        {"path": "/library/Author/Book/book.epub", "format": "epub", "size": 1234}
    ]
    await login_member(client)
    assert (await browse(client, work_id=work_id))["items"] == []


async def test_bookshelf_leaves_out_books_whose_only_copies_are_gone(client, admin, database):
    async with database() as db, db.begin():
        await add(db, "Still on disk")
        await add(db, "Deleted file", state="intentionally-removed")
        await add(db, "Lost file", state="missing-confirmed")
        await add(db, "Same title")
        await add(db, "Same title", state="intentionally-removed")
        reorganised, _, _ = await add(db, "Reorganised copy")
        old_library = await add_owned(db, reorganised)
        await db.execute(
            update(LibraryAsset)
            .where(LibraryAsset.library_id == old_library.id)
            .values(state="intentionally-removed")
        )
    books = (await client.get("/api/library/books")).json()
    assert sorted(item["title"] for item in books["items"]) == [
        "Reorganised copy",
        "Same title",
        "Still on disk",
    ]
    assert books["total"] == 3
    lost = (await client.get("/api/library/books", params={"state": "missing-confirmed"})).json()
    assert [item["title"] for item in lost["items"]] == ["Lost file"]
    assert (await browse(client))["total"] == 7
