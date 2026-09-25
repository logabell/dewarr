from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.models import LibraryAsset, LibraryGrant, Work
from tests.integration.test_discovery import add_owned, login_member

pytestmark = pytest.mark.integration


async def copies(db, *, language=None, audio_language=None, audio_author="Jojo Moyes"):
    ebook = Work(title="The Giver of Stars", authors=["Jojo Moyes"], language=language)
    audio = Work(title="The Giver of Stars", authors=[audio_author], language=audio_language)
    db.add_all([ebook, audio])
    await db.flush()
    ebook_library = await add_owned(db, ebook)
    audio_library = await add_owned(db, audio)
    audio_asset = await db.scalar(
        select(LibraryAsset).where(LibraryAsset.library_id == audio_library.id)
    )
    audio_asset.medium = "audio"
    audio_asset.title = "The Giver of Stars"
    audio_asset.metadata_snapshot = {"narrators": ["Julia Whelan"], "cover_path": "audio.jpg"}
    ebook_asset = await db.scalar(
        select(LibraryAsset).where(LibraryAsset.library_id == ebook_library.id)
    )
    ebook_asset.metadata_snapshot = {"cover_path": "ebook.jpg"}
    await db.flush()
    return ebook, audio, ebook_library, audio_library


@pytest.mark.parametrize("languages", [(None, None), ("en", "english"), ("eng", " English ")])
async def test_group_before_pagination_filters_and_combine_details(
    client, admin, database, languages
):
    async with database() as db, db.begin():
        ebook, audio, _, audio_library = await copies(
            db, language=languages[0], audio_language=languages[1]
        )
        ebook_id, audio_id, library_id = ebook.id, audio.id, audio_library.id
    for endpoint in ["/api/catalog/works", "/api/library/books"]:
        for medium in ["any", "ebook", "audio"]:
            response = await client.get(
                endpoint, params={"q": "Giver", "medium": medium, "limit": 1}
            )
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["total"] == 1
            assert page["items"][0]["id"] == str(ebook_id)
            assert page["items"][0]["availability"]["ebook"]
            assert page["items"][0]["availability"]["audio"]
        assert (await client.get(endpoint, params={"offset": 1})).json()["items"] == []
    for endpoint in ["/api/discovery/library", "/api/discovery/local"]:
        response = await client.get(endpoint)
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
    filtered = await client.get(
        "/api/library/books", params={"library_id": str(library_id), "q": "Julia"}
    )
    assert filtered.json()["total"] == 1
    for work_id in [ebook_id, audio_id]:
        detail = (await client.get(f"/api/catalog/works/{work_id}")).json()
        assert detail["availability"]["ebook"] and detail["availability"]["audio"]
        assets = (await client.get("/api/library/assets", params={"work_id": str(work_id)})).json()
        assert assets["total"] == 2
    async with database() as db:
        assert (await db.get(Work, audio_id)).redirect_to is None


async def test_grouped_covers_prefer_requested_format_and_fall_back(
    client, admin, database, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from fastapi import HTTPException
    from sqlalchemy import update

    from app.db.models import ProviderCache
    from app.domain import library_covers

    async with database() as db, db.begin():
        ebook, audio, ebook_library, audio_library = await copies(db)
        ebook_id, audio_id = ebook.id, audio.id
        bases = {}
        from app.db.models import Integration
        from app.security import encrypt_secrets

        for name, library in [("ebook", ebook_library), ("audio", audio_library)]:
            integration = await db.get(Integration, library.integration_id)
            integration.base_url = f"http://{name}.invalid"
            integration.encrypted_secrets = encrypt_secrets({"token": "fixture"})
            bases[integration.base_url] = name
    calls = []
    fail = False

    async def fetch(base, token, item, **_extra):
        calls.append(bases[base])
        if fail and bases[base] == "ebook":
            raise HTTPException(404)
        return bases[base].encode(), "image/jpeg"

    monkeypatch.setattr(library_covers, "fetch_cover", fetch)
    assert (await client.get(f"/api/catalog/works/{audio_id}/cover")).content == b"ebook"
    assert (
        await client.get(f"/api/catalog/works/{ebook_id}/cover?medium=audio")
    ).content == b"audio"
    fail = True
    assert (await client.get(f"/api/catalog/works/{ebook_id}/cover")).content == b"ebook"
    assert calls == ["ebook", "audio"]
    async with database() as db, db.begin():
        await db.execute(
            update(ProviderCache).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await client.get(f"/api/catalog/works/{ebook_id}/cover")).content == b"audio"
    assert calls == ["ebook", "audio", "ebook", "audio"]


@pytest.mark.parametrize(
    "values", [{"audio_author": "Another Author"}, {"language": "en", "audio_language": "fr"}]
)
async def test_distinct_authors_and_languages_stay_separate(client, admin, database, values):
    async with database() as db, db.begin():
        await copies(db, **values)
    assert (await client.get("/api/catalog/works")).json()["total"] == 2


async def test_grouping_does_not_expose_ungranted_formats(client, admin, database):
    async with database() as db, db.begin():
        ebook, audio, ebook_library, _ = await copies(db)
        ebook.catalog_public = audio.catalog_public = False
        ebook_id, library_id = ebook.id, ebook_library.id
    member = await login_member(client)
    async with database() as db, db.begin():
        db.add(LibraryGrant(user_id=UUID(str(member)), library_id=library_id))
    page = (await client.get("/api/catalog/works")).json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == str(ebook_id)
    assert page["items"][0]["availability"]["ebook"]
    assert not page["items"][0]["availability"]["audio"]
    assert (await client.get("/api/library/assets", params={"work_id": str(ebook_id)})).json()[
        "total"
    ] == 1


async def test_case_whitespace_and_unknown_language_are_compatible(client, admin, database):
    async with database() as db, db.begin():
        ebook, audio, _, _ = await copies(db, language="en", audio_author="  JOJO   MOYES  ")
        audio.title = "  THE GIVER   OF STARS "
    assert (await client.get("/api/catalog/works")).json()["total"] == 1


async def test_repeated_and_empty_author_credits_do_not_split_a_book(client, admin, database):
    async with database() as db, db.begin():
        _, audio, _, _ = await copies(db)
        audio.authors = ["Jojo Moyes", " JOJO MOYES ", ""]
    assert (await client.get("/api/catalog/works")).json()["total"] == 1


async def test_unknown_language_does_not_bridge_translations(client, admin, database):
    async with database() as db, db.begin():
        await copies(db, language="en", audio_language="fr")
        db.add(Work(title="The Giver of Stars", authors=["Jojo Moyes"]))
    assert (await client.get("/api/catalog/works")).json()["total"] == 3


async def test_rejected_identity_is_not_regrouped(client, admin, database):
    async with database() as db, db.begin():
        _, audio, _, _ = await copies(db)
        audio.metadata_fields = {"identity_rejected": True}
    assert (await client.get("/api/catalog/works")).json()["total"] == 2


@pytest.mark.parametrize(
    "provider_title",
    [
        "The Giver of Stars",
        "The Giver of Stars: Reese's Book Club: A Novel",
    ],
)
async def test_unlinked_hardcover_search_and_preview_inherit_combined_ownership(
    client, admin, database, monkeypatch, provider_title
):
    from sqlalchemy import func

    from app.adapters.catalog_types import BookData, SearchPage
    from app.api import metadata
    from app.db.models import WorkMetadataSource

    async with database() as db, db.begin():
        ebook, _, _, _ = await copies(db, language="en", audio_language="english")
        ebook_id = str(ebook.id)
    book = BookData(
        provider="hardcover",
        external_id="987",
        title=provider_title,
        authors=["Jojo Moyes"],
        language="eng",
    )

    async def call(db, user_id, provider, action, *args, **kwargs):
        return (
            (
                SearchPage(provider=provider, items=[book], page=1, has_more=False)
                if action == "search"
                else book
            ),
            False,
            None,
        )

    monkeypatch.setattr(metadata, "provider_call", call)
    search = await client.get(
        "/api/metadata/search", params={"q": "The Giver of Stars", "provider": "hardcover"}
    )
    assert search.status_code == 200, search.text
    work = search.json()["known_works"]["987"]
    assert work["id"] == ebook_id
    assert work["availability"]["ebook"] and work["availability"]["audio"]
    preview = await client.get("/api/metadata/books/hardcover/987")
    assert preview.status_code == 200, preview.text
    assert preview.json()["work"] == work
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(WorkMetadataSource)) == 0


@pytest.mark.parametrize(
    "case",
    [
        "rejected",
        "ambiguous",
        "other-author",
        "no-author",
        "blank-author",
        "other-language",
        "private",
    ],
)
async def test_provider_display_fallback_respects_identity_and_visibility(
    client, admin, database, case
):
    from datetime import UTC, datetime

    from app.adapters.catalog_types import BookData
    from app.db.models import User, WorkMetadataSource
    from app.domain.catalog_bindings import displayed_provider_works

    async with database() as db, db.begin():
        ebook, audio, _, _ = await copies(db, language="en", audio_language="english")
        if case == "blank-author":
            ebook.authors = audio.authors = ["  "]
        if case == "rejected":
            db.add(
                WorkMetadataSource(
                    work_id=ebook.id,
                    provider="hardcover",
                    external_id="987",
                    accepted=False,
                    snapshot={},
                    fetched_at=datetime.now(UTC),
                )
            )
        if case == "ambiguous":
            db.add(Work(title=ebook.title, authors=ebook.authors, language="fr"))
        if case == "private":
            ebook.catalog_public = audio.catalog_public = False
    user_id = await login_member(client) if case == "private" else UUID(admin["id"])
    book = BookData(
        provider="hardcover",
        external_id="987",
        title="The Giver of Stars",
        authors=["  "]
        if case == "blank-author"
        else []
        if case == "no-author"
        else ["Different Writer"]
        if case == "other-author"
        else ["Jojo Moyes"],
        language="fr" if case == "other-language" else None,
    )
    async with database() as db:
        user = await db.get(User, user_id)
        assert await displayed_provider_works(db, user, "hardcover", [book]) == {}


@pytest.mark.parametrize(
    "audio_title, total",
    [
        ("The Giver of Stars: Reese's Book Club: A Novel", 1),
        ("The Giver of Stars: Study Guide", 2),
        ("The Giver of Stars: Volume Two", 2),
        ("The Giver of Stars: A Study Guide", 2),
        ("The Giver of Stars: Dramatized Adaptation", 2),
        ("The Giver of Stars: The Graphic Novel", 2),
        ("The Giver of Stars (Unabridged) (read by Julia Whelan)", 1),
        ("The Giver of Stars (read by Julia Whelan) (Unabridged): A Novel", 1),
        ("Summary of The Giver of Stars: A Novel", 2),
    ],
)
async def test_only_edition_labels_are_ignored(client, admin, database, audio_title, total):
    async with database() as db, db.begin():
        _, audio, _, _ = await copies(db, language="en", audio_language="english")
        audio.title = audio_title
    assert (await client.get("/api/catalog/works")).json()["total"] == total


async def test_scoped_grouping_keeps_ambiguity_and_redirect_families(client, admin, database):
    from app.db.models import User
    from app.domain.catalog_display import display_map

    async with database() as db, db.begin():
        titles = ["Journey", "Journey: Home", "Journey: Away", "Harbor", "Harbor (Unabridged)"]
        works = [Work(title=title, authors=["Writer"], language="en") for title in titles]
        db.add_all(works)
        await db.flush()
        for work in works:
            await add_owned(db, work)
        alias = Work(title="Old Harbor Title", authors=["Writer"], redirect_to=works[3].id)
        db.add(alias)
        await db.flush()
        ids = [work.id for work in works] + [alias.id]
    async with database() as db:
        user = await db.get(User, UUID(admin["id"]))
        full = dict((await db.execute(select(display_map(user)))).all())
        for requested in ([ids[0]], [ids[1]], [ids[3]], [ids[5]], [ids[1], ids[4]]):
            scoped = dict((await db.execute(select(display_map(user, requested)))).all())
            expected_roots = {full[key] for key in requested}
            assert all(scoped[key] == full[key] for key in requested)
            assert {key: root for key, root in scoped.items() if root in expected_roots} == {
                key: root for key, root in full.items() if root in expected_roots
            }


@pytest.mark.parametrize("path", ["/api/catalog/works", "/api/library/books"])
async def test_grouped_page_totals_survive_empty_offsets(client, admin, database, path):
    async with database() as db, db.begin():
        await copies(db)
    first = (await client.get(path, params={"limit": 1})).json()
    past = (await client.get(path, params={"offset": 500, "limit": 1})).json()
    empty = (await client.get(path, params={"q": "No such book"})).json()
    assert first["total"] == past["total"] == 1
    assert len(first["items"]) == 1 and past["items"] == []
    assert empty["total"] == 0 and empty["items"] == []
