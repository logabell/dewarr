# ruff: noqa: F811
"""Synthetic files exercise per-book matching/import for an incomplete author pack.

Titles mirror the observed Dan Brown pack; bytes, ISBNs and catalog transport
fixtures are synthetic. No tracker or live Hardcover/download client is used.
"""

import hashlib

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import LibraryAsset, ProviderObject, Version, Work, WorkMetadataSource
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_execution import ready_route  # noqa: F401
from tests.integration.test_import_execution import start as start_import
from tests.integration.test_import_inspections import run_worker, submit
from tests.integration.test_inspection_matching import edition, matches
from tests.media_fixtures import audio, epub

pytestmark = pytest.mark.integration


async def authored_edition(database, title, isbn, *, author="Dan Brown"):
    record = await edition(database, title=title, identifiers={"isbn_13": isbn})
    async with database() as db, db.begin():
        (await db.get(Work, record["work"])).authors = [author]
        source = await db.get(WorkMetadataSource, record["source"])
        source.snapshot = {**source.snapshot, "authors": [author]}
    return record


def synthetic_isbn(number):
    base = f"978000000{number:03}"
    checksum = (-sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(base))) % 10
    return base + str(checksum)


async def inspect_pack(client, path):
    response = await submit(client, key=f"inspect-{path}", path=path)
    assert response.status_code == 202, response.text
    await run_worker()
    inspection = (await client.get(f"/api/organization/inspections/{response.json()['id']}")).json()
    assert inspection["state"] == "ready", inspection
    grouping = (
        await client.get(f"/api/organization/inspections/{inspection['id']}/grouping")
    ).json()
    return inspection, grouping


async def test_five_book_author_subset_imports_individually_without_filling_author_catalog(
    client, admin, database, ready_route
):
    route = ready_route
    titles = [
        "Digital Fortress",
        "Deception Point",
        "Angels & Demons",
        "The Da Vinci Code",
        "The Lost Symbol",
    ]
    records = []
    originals = {}
    for index, title in enumerate(titles):
        isbn = synthetic_isbn(index)
        records.append(await authored_edition(database, title, isbn))
        path = route["source"] / "author-pack" / f"{index + 1:02}.epub"
        # File tags, not the generic filename or the source description, identify it.
        epub(path, title=title.replace("&", "and"), author="Dan Brown", isbn=isbn)
        originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    absent = await authored_edition(database, "Origin", synthetic_isbn(9))
    inspection, grouping = await inspect_pack(client, "author-pack")
    report = await matches(client, (inspection, grouping))
    assert report["total"] == 5
    assert all(m["status"] == "matched" for m in report["items"]), report
    selected = {m["selected_version_id"] for m in report["items"]}
    assert selected == {str(r["version"]) for r in records}
    settings = (await client.get("/api/organization/settings")).json()
    command = {
        "inspection_revision": inspection["snapshot"]["revision"],
        "grouping_revision": grouping["revision"],
        "profile_revision": settings["revision"],
        "selections": [
            {
                "group_key": m["group_key"],
                "work_id": next(
                    c["work_id"]
                    for c in m["candidates"]
                    if c["version_id"] == m["selected_version_id"]
                ),
                "version_id": m["selected_version_id"],
                "full_content": True,
                "match_revision": m["revision"],
            }
            for m in report["items"]
        ],
    }
    response = await client.post(
        f"/api/organization/inspections/{inspection['id']}/plans", json=command
    )
    assert response.status_code == 201, response.text
    route["plan"], route["plan_id"] = response.json(), response.json()["id"]
    response = await start_import(client, route, key="five-book-author-subset")
    assert response.status_code == 202, response.text
    import_id = response.json()["id"]
    await run_worker()
    result = (await client.get(f"/api/organization/imports/{import_id}")).json()
    assert len(result["entries"]) == 5 and all(
        e["state"] == "confirmed" for e in result["entries"]
    ), result
    for record in records:
        book = (await client.get(f"/api/catalog/works/{record['work']}")).json()
        assert book["availability"]["owned"]
        async with database() as db:
            asset = await db.scalar(
                select(LibraryAsset).where(LibraryAsset.version_id == record["version"])
            )
            provider = await db.get(ProviderObject, record["provider"])
            assert asset and asset.match_status == "matched"
            assert provider.work_id == record["work"] and provider.version_id == asset.version_id
    assert not (await client.get(f"/api/catalog/works/{absent['work']}")).json()["availability"][
        "owned"
    ]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in originals} == originals
    # Replaying the import command must not create five more entries.
    assert (await start_import(client, route, key="five-book-author-subset")).json()[
        "id"
    ] == import_id


@pytest.mark.parametrize(
    "case", ["title-only", "wrong-author", "conflicting-isbn", "duplicate-edition"]
)
async def test_connector_variant_does_not_bypass_import_identity_review(
    client, admin, database, tmp_path, monkeypatch, case
):
    isbn = synthetic_isbn(1)
    record = await authored_edition(database, "Angels & Demons", isbn)
    if case == "duplicate-edition":
        await edition(
            database, work_id=record["work"], title="Angels & Demons", identifiers={"isbn_13": isbn}
        )
    epub(
        tmp_path / "pack/book.epub",
        title="Angels and Demons",
        author="Different Writer" if case == "wrong-author" else "Dan Brown",
        isbn=None
        if case == "title-only"
        else synthetic_isbn(2)
        if case == "conflicting-isbn"
        else isbn,
    )
    monkeypatch.setattr(get_settings(), "import_sources", {"fixture": tmp_path.resolve()})
    inspected = await inspect_pack(client, "pack")
    result = (await matches(client, inspected))["items"][0]
    assert result["status"] == "review" and not result["selected_version_id"], result
    assert str(record["version"]) in {c["version_id"] for c in result["candidates"]}


@pytest.mark.parametrize("wrong_abridgment", [False, True])
async def test_two_recordings_of_one_pack_book_keep_narrator_and_abridgment_identity(
    client, admin, database, tmp_path, monkeypatch, wrong_abridgment
):
    versions = []
    work_id = None
    for index, narrator in enumerate(["Garrick Hagon", "Bruce Huntey"]):
        asin = f"B{index + 1:09}"
        record = await edition(
            database, work_id=work_id, title="The Stand", medium="audio", identifiers={"asin": asin}
        )
        work_id = record["work"]
        async with database() as db, db.begin():
            (await db.get(Work, work_id)).authors = ["Stephen King"]
            version = await db.get(Version, record["version"])
            version.narrators, version.abridged = [narrator], False
        versions.append(record["version"])
        audio(
            tmp_path / "pack" / narrator / "book.mp3",
            title="The Stand",
            author="Stephen King",
            narrator=narrator,
            tags={
                "asin": asin,
                "language": "en",
                "abridged": "true" if wrong_abridgment and index else "false",
            },
        )
    monkeypatch.setattr(get_settings(), "import_sources", {"fixture": tmp_path.resolve()})
    inspected = await inspect_pack(client, "pack")
    report = await matches(client, inspected)
    assert report["total"] == 2
    selected = {m["selected_version_id"] for m in report["items"] if m["status"] == "matched"}
    assert selected == {str(v) for v in (versions[:1] if wrong_abridgment else versions)}
    if wrong_abridgment:
        held = next(m for m in report["items"] if m["status"] == "review")
        assert "Abridgment is unknown or differs" in held["message"]
