# ruff: noqa: F401, F811
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.models import (
    CatalogSeries,
    LibraryGrant,
    Operation,
    SeriesMembership,
    Version,
    Work,
    WorkMetadataSource,
)
from tests.integration.test_discovery import add_owned, login_member
from tests.integration.test_metadata import connect, provider

pytestmark = pytest.mark.integration


async def source(db, work, provider="hardcover", external_id="42", accepted=True, **snapshot):
    row = WorkMetadataSource(
        work_id=work.id,
        provider=provider,
        external_id=external_id,
        accepted=accepted,
        snapshot=snapshot,
        fetched_at=datetime.now(UTC),
    )
    db.add(row)
    return row


async def local(client, query):
    response = await client.get("/api/catalog/works", params={"q": query})
    assert response.status_code == 200, response.text
    return response.json()


async def test_identifier_discovery_matches_parser_aliases_without_crossing_namespaces(database):
    from app.domain.source_queries import edition_identifiers
    from app.importing.match_evidence import catalog_identifiers, identifier_matches_sql

    values = [
        {"ISBN-13": "9781234567897"},
        {" ISBN_10 ": "123456789X"},
        {"ＩＳＢＮ": "9781234567897"},
        {"ASIN": "b012345678"},
        {" AsIn ": " urn:asin:B012345678 "},
        {"uuid": "123456789X"},
        {"uuid": " urn:isbn:123456789X "},
        {"uuid": " urn:asin:B012345678 "},
        {"asin": "ISBN-13:9781234567897"},
        {"isbn": "ASIN:B012345678"},
        {"isbn": 9781234567897},
        {"asin": ["B012345678"]},
        {"isbn": "978.1234567897"},
        {"asin": "B0123-45678"},
        {"": "123456789X"},
        {"": "B012345678"},
    ]
    async with database() as db, db.begin():
        work = Work(title="Identifier cases", authors=["Writer"])
        db.add(work)
        await db.flush()
        versions = [Version(work_id=work.id, medium="ebook", identifiers=value) for value in values]
        db.add_all(versions)
        await db.flush()
        for assertion in [("isbn", "9781234567897"), ("asin", "B012345678")]:
            observed = set(
                await db.scalars(
                    select(Version.id).where(
                        identifier_matches_sql(Version.identifiers, [assertion])
                    )
                )
            )
            expected = {
                version.id
                for version in versions
                if assertion in catalog_identifiers(version.identifiers)
            }
            assert observed == expected, assertion
        assert set(await edition_identifiers(db, work, "ebook")) == {"9781234567897", "B012345678"}
        assert await edition_identifiers(db, work, "audio") == []


async def test_connector_search_variants_cover_titles_editions_and_series_without_leaks(
    client, admin, database
):
    async with database() as db, db.begin():
        work = Work(title="Angels & Demons", authors=["Dan Brown"])
        edition_work = Work(title="Translated title", authors=["Writer"])
        series_work = Work(title="Coastal volume", authors=["Writer"])
        private = Work(
            title="Private & Hidden",
            authors=[],
            catalog_public=False,
            catalog_owner_id=UUID(admin["id"]),
        )
        db.add_all([work, edition_work, series_work, private])
        await db.flush()
        db.add(Version(work_id=edition_work.id, title="Salt and Sea", medium="ebook"))
        await source(db, series_work, series=[{"name": "Coast & Country"}])
        ids = {"work": str(work.id), "edition": str(edition_work.id), "series": str(series_work.id)}
    await login_member(client)
    for query, key in [
        ("Angels and Demons", "work"),
        ("Angels ＆ Demons (Unabridged)", "work"),
        ("Salt & Sea", "edition"),
        ("Coast and Country", "series"),
    ]:
        result = await local(client, query)
        assert result["total"] == 1, query
        assert [item["id"] for item in result["items"]] == [ids[key]], query
    for query in ["Angels Demons", "Private and Hidden", "Salt & %", "Coast and _"]:
        assert (await local(client, query))["total"] == 0, query
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0


async def test_series_and_identifiers_find_canonical_work_without_acquiring(
    client, admin, database
):
    async with database() as db, db.begin():
        work = Work(title="First volume", authors=["Example Writer"])
        origin = Work(title="Old title", authors=["Prior name"])
        db.add_all([work, origin])
        await db.flush()
        origin.redirect_to = work.id
        await source(db, origin, series=[{"name": "Harbor Chronicles", "external_id": "9"}])
        await source(db, work, provider="openlibrary", external_id="OL1W")
        db.add(
            Version(
                work_id=origin.id,
                medium="ebook",
                identifiers={
                    "ISBN-10": "\tISBN-10:0-306-40615-2\n",
                    " ASIN ": "  urn:asin:b012345678  ",
                },
            )
        )
        series = CatalogSeries(
            owner_id=UUID(admin["id"]),
            provider="hardcover",
            external_id="17",
            name="Observed cycle",
            fetched_at=datetime.now(UTC),
        )
        db.add(series)
        await db.flush()
        db.add(
            SeriesMembership(
                series_id=series.id, external_id="42", work_id=origin.id, snapshot={}, present=True
            )
        )
        work_id = str(work.id)
    for query in [
        "First volume",
        "Prior name",
        "Harbor",
        "Observed cycle",
        "hardcover:42",
        "openlibrary:ol1w",
        "9780306406157",
        "ISBN: 978-0-306-40615-7",
        "0306406152",
        "ASIN:B012345678",
    ]:
        result = await local(client, query)
        assert result["total"] == 1 and result["items"][0]["id"] == work_id, query
    assert (await local(client, "9780306406158"))["total"] == 0
    assert (await local(client, "%"))["total"] == 0
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0


async def test_search_does_not_expose_private_origins_rejected_series_or_other_accounts(
    client, admin, database
):
    async with database() as db, db.begin():
        public = Work(title="Public book", authors=[])
        private = Work(
            title="Private alias",
            authors=[],
            catalog_public=False,
            catalog_owner_id=UUID(admin["id"]),
        )
        db.add_all([public, private])
        await db.flush()
        private.redirect_to = public.id
        await source(db, private, series=[{"name": "Hidden series"}])
        await source(
            db, public, external_id="99", accepted=False, series=[{"name": "Rejected series"}]
        )
        # A title/description in a metadata snapshot is not a series-name match.
        await source(db, public, external_id="98", description="Unrelated phrase", series=None)
        db.add(Version(work_id=private.id, medium="ebook", identifiers={"isbn": "9780306406157"}))
        series = CatalogSeries(
            owner_id=UUID(admin["id"]),
            provider="hardcover",
            external_id="17",
            name="Personal series",
            fetched_at=datetime.now(UTC),
        )
        db.add(series)
        await db.flush()
        db.add(
            SeriesMembership(
                series_id=series.id, external_id="98", work_id=public.id, snapshot={}, present=True
            )
        )
    await login_member(client)
    for query in [
        "Private alias",
        "Hidden series",
        "Rejected series",
        "Personal series",
        "Unrelated phrase",
        "9780306406157",
        "hardcover:42",
        "hardcover:99",
    ]:
        assert (await local(client, query))["total"] == 0, query
    assert (await local(client, "Public book"))["total"] == 1


async def test_provider_cache_keeps_ownership_scoped_and_fresh(client, admin, database, provider):
    await connect(client)
    async with database() as db, db.begin():
        work = Work(title="Protected title", authors=["Writer"])
        db.add(work)
        await db.flush()
        await source(db, work)
        library = await add_owned(db, work)
        library_id, work_id = library.id, str(work.id)

    async def search():
        response = await client.get(
            "/api/metadata/search", params={"q": "Book", "provider": "hardcover"}
        )
        assert response.status_code == 200, response.text
        return response.json()["known_works"]

    known = await search()
    assert known["42"]["id"] == work_id and known["42"]["availability"]["ebook"]
    assert known["42"]["title"] == "Protected title"
    preview = (await client.get("/api/metadata/books/hardcover/42")).json()
    assert preview["work"]["id"] == work_id
    member = await login_member(client)
    await connect(client)
    assert not (await search())["42"]["availability"]["owned"]
    calls = len(provider["calls"])
    async with database() as db, db.begin():
        db.add(LibraryGrant(user_id=member, library_id=library_id))
    assert (await search())["42"]["availability"]["owned"]
    assert len(provider["calls"]) == calls  # Availability is never cached with provider data.


@pytest.mark.parametrize("kind", ["private", "rejected", "ambiguous", "same-title"])
async def test_provider_binding_requires_unique_accepted_visible_identity(
    client, admin, database, provider, kind
):
    await connect(client)
    async with database() as db, db.begin():
        work = Work(
            title="A Catalog Book",
            authors=["Writer"],
            catalog_public=kind != "private",
            catalog_owner_id=UUID(admin["id"]) if kind == "private" else None,
        )
        db.add(work)
        await db.flush()
        if kind != "same-title":
            await source(db, work, accepted=kind != "rejected")
        if kind == "ambiguous":
            another = Work(title="Another edition claim", authors=[])
            db.add(another)
            await db.flush()
            await source(db, another)
    if kind == "private":
        await login_member(client)
        await connect(client)
    response = await client.get(
        "/api/metadata/search", params={"q": "Book", "provider": "hardcover"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["known_works"] == {}
    assert (await client.get("/api/metadata/books/hardcover/42")).json()["work"] is None


async def test_provider_bindings_are_namespaced_and_follow_merges(
    client, admin, database, provider
):
    async with database() as db, db.begin():
        work = Work(title="Local fallback", authors=[])
        root = Work(title="Canonical fallback", authors=[])
        db.add_all([work, root])
        await db.flush()
        work.redirect_to = root.id
        await source(db, work, provider="openlibrary", external_id="OL1W")
        other = Work(title="Different provider", authors=[])
        db.add(other)
        await db.flush()
        await source(db, other, provider="hardcover", external_id="OL1W")
        root_id = str(root.id)
    response = await client.get("/api/metadata/search", params={"q": "Book"})
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "openlibrary"
    assert response.json()["known_works"]["OL1W"]["id"] == root_id
