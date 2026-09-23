import pytest

from app.adapters.audiobookshelf import ABSItem
from app.db.models import ProviderObject, Version, Work
from app.domain.identity import resolve_abs_version, resolve_abs_work, work_key

pytestmark = pytest.mark.integration


def item(title, authors):
    return ABSItem(
        id="shelf-item",
        library_id="library",
        title=title,
        authors=authors,
        narrators=["Scott Brick"],
    )


async def test_unabridged_shelf_title_matches_the_catalog_book_by_the_same_author(database):
    async with database() as db, db.begin():
        catalog = Work(
            title="Cloud Atlas",
            authors=["David Mitchell"],
            provisional=False,
            catalog_public=True,
            match_key=work_key("Cloud Atlas", ["David Mitchell"]),
        )
        duplicate = Work(
            title="Cloud Atlas (Unabridged)",
            authors=["David Mitchell"],
            provisional=True,
            catalog_public=False,
            match_key=work_key("Cloud Atlas (Unabridged)", ["David Mitchell"]),
        )
        other = Work(
            title="Cloud Atlas: The Graphic Novel",
            authors=["David Mitchell"],
            provisional=False,
            catalog_public=True,
            match_key=work_key("Cloud Atlas: The Graphic Novel", ["David Mitchell"]),
        )
        db.add_all([catalog, duplicate, other])
        await db.flush()
        link = ProviderObject(provider="abs:test", kind="item:audio", external_id="shelf-item")
        db.add(link)
        await db.flush()
        shelf = item("Cloud Atlas (Unabridged)", ["David Mitchell"])
        work = await resolve_abs_work(db, shelf, link)
        assert work.id == catalog.id and link.match_status == "matched"
        version = await resolve_abs_version(db, work, shelf, "audio", link)
        assert version.work_id == catalog.id and version.medium == "audio"
        separate = ProviderObject(provider="abs:test", kind="item:audio", external_id="graphic")
        db.add(separate)
        await db.flush()
        separate_item = item("Cloud Atlas: The Graphic Novel (Unabridged)", ["Someone Else"])
        created = await resolve_abs_work(db, separate_item, separate)
        assert created.id not in {catalog.id, other.id, duplicate.id}
        # The book is titled by the book. The recording keeps the library's label.
        assert created.title == "Cloud Atlas: The Graphic Novel"
        version = await resolve_abs_version(db, created, separate_item, "audio", separate)
        assert version.title == "Cloud Atlas: The Graphic Novel (Unabridged)"


async def test_existing_shelf_duplicate_joins_the_catalog_book(database):
    async with database() as db, db.begin():
        catalog = Work(
            title="Cloud Atlas",
            authors=["David Mitchell"],
            provisional=False,
            catalog_public=True,
            match_key=work_key("Cloud Atlas", ["David Mitchell"]),
        )
        duplicate = Work(
            title="Cloud Atlas (Unabridged)",
            authors=["David Mitchell"],
            provisional=True,
            catalog_public=False,
            match_key=work_key("Cloud Atlas (Unabridged)", ["David Mitchell"]),
            metadata_fields={"origin": "audiobookshelf", "fields": {}},
        )
        separate = Work(
            title="Cloud Atlas (Unabridged)",
            authors=["David Mitchell"],
            provisional=True,
            catalog_public=False,
            match_key=work_key("Cloud Atlas (Unabridged)", ["David Mitchell"]),
            metadata_fields={
                "origin": "audiobookshelf",
                "fields": {},
                "display_separate": True,
            },
        )
        db.add_all([catalog, duplicate, separate])
        await db.flush()
        shelf = item("Cloud Atlas (Unabridged)", ["David Mitchell"])
        snapshot = {"title": shelf.title, "authors": shelf.authors}
        link = ProviderObject(
            provider="abs:test",
            kind="item:audio",
            external_id="existing-duplicate",
            work_id=duplicate.id,
            snapshot=snapshot,
        )
        kept = ProviderObject(
            provider="abs:test",
            kind="item:audio",
            external_id="kept-separate",
            work_id=separate.id,
            snapshot=snapshot,
        )
        db.add_all([link, kept])
        await db.flush()
        moved = await resolve_abs_work(db, shelf, link)
        assert moved.id == catalog.id and link.work_id == catalog.id
        stayed = await resolve_abs_work(db, shelf, kept)
        assert stayed.id == separate.id and kept.work_id == separate.id


async def test_language_name_reuses_the_same_recording(database):
    async with database() as db, db.begin():
        work = Work(
            title="Cloud Atlas",
            authors=["David Mitchell"],
            language="en",
            provisional=False,
            catalog_public=True,
            match_key=work_key("Cloud Atlas", ["David Mitchell"]),
        )
        db.add(work)
        await db.flush()
        existing = Version(
            work_id=work.id,
            medium="audio",
            title="Cloud Atlas",
            language="en",
            narrators=["Scott Brick"],
            abridged=False,
            identifiers={"asin": "B00CLOUD01"},
        )
        db.add(existing)
        await db.flush()
        link = ProviderObject(provider="abs:test", kind="item:audio", external_id="language")
        db.add(link)
        await db.flush()
        shelf = item("Cloud Atlas (Unabridged)", ["David Mitchell"]).model_copy(
            update={"language": "eng", "abridged": False, "identifiers": {"asin": "B00CLOUD01"}}
        )
        found = await resolve_abs_version(db, work, shelf, "audio", link)
        assert found.id == existing.id


async def test_merged_shelf_copy_stays_on_the_surviving_book(database):
    async with database() as db, db.begin():
        catalog = Work(
            title="Cloud Atlas",
            authors=["David Mitchell"],
            provisional=False,
            catalog_public=True,
            match_key=work_key("Cloud Atlas", ["David Mitchell"]),
        )
        db.add(catalog)
        await db.flush()
        duplicate = Work(
            title="Cloud Atlas (Unabridged)",
            authors=["David Mitchell"],
            provisional=True,
            catalog_public=False,
            redirect_to=catalog.id,
            match_key=work_key("Cloud Atlas (Unabridged)", ["David Mitchell"]),
            metadata_fields={"origin": "audiobookshelf", "fields": {}},
        )
        db.add(duplicate)
        await db.flush()
        link = ProviderObject(provider="abs:test", kind="item:audio", external_id="merged")
        db.add(link)
        await db.flush()
        shelf = item("Cloud Atlas (Unabridged)", ["David Mitchell"])
        work = await resolve_abs_work(db, shelf, link)
        assert work.id == catalog.id and link.work_id == catalog.id


async def test_shelf_isbn_reuses_an_edition_stored_as_isbn_13(database):
    async with database() as db, db.begin():
        work = Work(
            title="Cloud Atlas",
            authors=["David Mitchell"],
            language="en",
            provisional=False,
            catalog_public=True,
            match_key=work_key("Cloud Atlas", ["David Mitchell"]),
        )
        db.add(work)
        await db.flush()
        existing = Version(
            work_id=work.id,
            medium="audio",
            title="Cloud Atlas",
            language="en",
            narrators=["Scott Brick"],
            abridged=False,
            identifiers={"isbn_13": "9780812994735"},
        )
        db.add(existing)
        await db.flush()
        link = ProviderObject(provider="abs:test", kind="item:audio", external_id="isbn")
        db.add(link)
        await db.flush()
        shelf = item("Cloud Atlas (Unabridged)", ["David Mitchell"]).model_copy(
            update={"language": "en", "abridged": False, "identifiers": {"isbn": "9780812994735"}}
        )
        found = await resolve_abs_version(db, work, shelf, "audio", link)
        assert found.id == existing.id
