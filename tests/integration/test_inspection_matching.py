import json
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select

from app.adapters.catalog_types import BookData, EditionData
from app.config import get_settings
from app.db.models import (
    DownloadInspection,
    FrozenImportPlan,
    ProviderObject,
    User,
    Version,
    Work,
    WorkMetadataSource,
)
from app.jobs.queue import get_queue
from tests.integration.test_import_inspections import run_worker, submit
from tests.media_fixtures import audio, epub

pytestmark = pytest.mark.integration


async def test_many_editions_reuse_canonical_lookup_but_refresh_the_next_match(
    client, admin, database, inspected, tmp_path
):
    from app.importing.inspection import InspectedGroup
    from app.importing.matching import match_group

    chosen = await edition(database)
    for index in range(39):
        await edition(database, work_id=chosen["work"], identifiers={"asin": f"B{index:09}"})
    async with database() as db, db.begin():
        root = Work(title="First Harbor", authors=["Alex Morgan"], language="en")
        db.add(root)
        await db.flush()
        (await db.get(Work, chosen["work"])).redirect_to = root.id
        root_id = root.id
    inspection, grouping = inspected
    group = InspectedGroup.model_validate(grouping["content"]["groups"][0])
    async with database() as db:
        calls = []

        def count(*args):
            calls.append(1)

        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", count)
        started = time.perf_counter()
        try:
            match = await match_group(db, inspection["snapshot"], grouping["revision"], group)
        finally:
            elapsed = time.perf_counter() - started
            event.remove(engine, "before_cursor_execute", count)
        print(f"40 editions: {len(calls)} SQL statements in {elapsed:.4f}s")
        (tmp_path / "import-metrics.json").write_text(
            json.dumps(
                {
                    "editions": len(match.candidates),
                    "sql_statements": len(calls),
                    "elapsed_seconds": elapsed,
                }
            )
        )
        assert len(match.candidates) == 40
        assert match.status == "matched" and match.selected_version_id == chosen["version"]
        assert all(candidate.work_id == root_id for candidate in match.candidates)
        assert len(calls) <= 3, f"Edition matching issued {len(calls)} SQL statements"
        async with database() as other, other.begin():
            (await other.get(Work, root_id)).metadata_fields = {"identity_rejected": True}
        fresh = await match_group(db, inspection["snapshot"], grouping["revision"], group)
        assert fresh.status == "review" and fresh.selected_version_id is None
        assert fresh.revision != match.revision


async def edition(
    database, *, work_id=None, title="First Harbor", identifiers=None, medium="ebook"
):
    async with database() as db, db.begin():
        work = (
            await db.get(Work, work_id)
            if work_id
            else Work(title=title, authors=["Alex Morgan"], language="en")
        )
        db.add(work)
        await db.flush()
        data = EditionData(
            external_id=str(uuid4()),
            title=title,
            medium=medium,
            language="en",
            identifiers=identifiers or {"isbn_13": "9781234567897"},
        )
        source = WorkMetadataSource(
            work_id=work.id,
            provider="hardcover",
            external_id=str(uuid4()),
            accepted=True,
            fetched_at=datetime.now(UTC),
            snapshot=BookData(
                provider="hardcover",
                external_id="100",
                title=title,
                authors=work.authors,
                editions=[data],
            ).model_dump(mode="json"),
        )
        version = Version(
            work_id=work.id,
            title=title,
            medium=medium,
            language="en",
            identifiers=data.identifiers,
            narrators=[],
        )
        db.add_all([source, version])
        await db.flush()
        provider = ProviderObject(
            provider=f"hardcover:{work.id}",
            kind="edition",
            external_id=data.external_id,
            work_id=work.id,
            version_id=version.id,
            metadata_source_id=source.id,
            snapshot=data.model_dump(mode="json"),
            match_status="matched",
        )
        db.add(provider)
        await db.flush()
        return {
            "work": work.id,
            "version": version.id,
            "provider": provider.id,
            "source": source.id,
        }


@pytest.fixture
async def inspected(client, admin, database, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    epub(root / "pack/book.epub", isbn="urn:isbn:1-234-56789-X")
    monkeypatch.setattr(get_settings(), "import_sources", {"fixture": root})
    request = await submit(client)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    inspection = (await client.get(f"/api/organization/inspections/{request.json()['id']}")).json()
    grouping = (
        await client.get(f"/api/organization/inspections/{inspection['id']}/grouping")
    ).json()
    return inspection, grouping


async def matches(client, inspected, **params):
    inspection, grouping = inspected
    response = await client.get(
        f"/api/organization/inspections/{inspection['id']}/matches",
        params={"grouping_revision": grouping["revision"], **params},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def freeze(client, inspected, match, *, version_id=None, complete=True):
    inspection, grouping = inspected
    candidate = next(
        row for row in match["candidates"] if row["version_id"] == match["selected_version_id"]
    )
    settings = (await client.get("/api/organization/settings")).json()
    return await client.post(
        f"/api/organization/inspections/{inspection['id']}/plans",
        json={
            "inspection_revision": inspection["snapshot"]["revision"],
            "grouping_revision": grouping["revision"],
            "profile_revision": settings["revision"],
            "selections": [
                {
                    "group_key": match["group_key"],
                    "work_id": candidate["work_id"],
                    "version_id": version_id or candidate["version_id"],
                    "full_content": complete,
                    "match_revision": match["revision"],
                }
            ],
        },
    )


async def test_local_identifier_match_freezes_replayable_evidence_without_claiming_ownership(
    client, admin, database, inspected
):
    chosen = await edition(database)
    before = (await client.get(f"/api/catalog/works/{chosen['work']}")).json()
    assert not before["availability"]["owned"]
    report = await matches(client, inspected)
    match = report["items"][0]
    assert match["status"] == "matched" and match["selected_version_id"] == str(chosen["version"])
    assert match["evidence"]["identifiers"] == [{"namespace": "isbn", "value": "9781234567897"}]
    assert (await matches(client, inspected))["items"][0] == match
    planned = await freeze(client, inspected, match, complete=False)
    assert planned.status_code == 201, planned.text
    document = planned.json()["document"]
    assert document["matching_evidence"][document["groups"][0]["id"]] == match
    assert document["plan"]["expected_items"] == 0  # Identity alone never asserts completeness.
    ready = await freeze(client, inspected, match)
    assert ready.status_code == 201, ready.text
    assert ready.json()["document"]["plan"]["expected_items"] == 1
    assert ready.json()["document"]["groups"][0]["metadata"]["isbn"] == "9781234567897"
    assert (
        "9781234567897"
        in next(iter(ready.json()["document"]["initial_sidecars"].values()))["metadata.opf"]
    )
    assert (await freeze(client, inspected, match)).json()["id"] == ready.json()["id"]
    assert not (await client.get(f"/api/catalog/works/{chosen['work']}")).json()["availability"][
        "owned"
    ]


@pytest.mark.parametrize("change", ["version", "work", "pending", "withdrawn", "duplicate"])
async def test_catalog_changes_require_refresh_before_freezing_automatic_selection(
    client, admin, database, inspected, change
):
    chosen = await edition(database)
    match = (await matches(client, inspected))["items"][0]
    if change == "duplicate":
        await edition(database, work_id=chosen["work"])
    else:
        async with database() as db, db.begin():
            if change == "version":
                (await db.get(Version, chosen["version"])).publication_year = 2025
            elif change == "work":
                (await db.get(Work, chosen["work"])).authors = ["Changed contributor"]
            elif change == "pending":
                (await db.get(ProviderObject, chosen["provider"])).match_status = "needs-review"
            else:
                (await db.get(WorkMetadataSource, chosen["source"])).accepted = False
    result = await freeze(client, inspected, match)
    assert result.status_code == 409, result.text
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(FrozenImportPlan)) == 0


async def test_duplicate_identifier_and_title_only_candidates_never_auto_select(
    client, admin, database, inspected
):
    one = await edition(database)
    two = await edition(database)
    match = (await matches(client, inspected))["items"][0]
    assert match["status"] == "review" and not match["selected_version_id"]
    assert "multiple catalog versions" in match["message"]
    async with database() as db, db.begin():
        (await db.get(Version, one["version"])).identifiers = {}
        (await db.get(Version, two["version"])).identifiers = {}
    match = (await matches(client, inspected))["items"][0]
    assert match["status"] == "review" and not any(
        row["identifier_match"] for row in match["candidates"]
    )


@pytest.mark.parametrize(
    "identifier_value", ["  ISBN-13:9781234567897  ", "\turn:isbn-13:9781234567897\n"]
)
@pytest.mark.parametrize("identifier_key", ["isbn_13", "ISBN-13", " ISBN_13 "])
async def test_qualified_duplicate_identifier_cannot_hide_behind_a_different_title(
    client, admin, database, inspected, identifier_value, identifier_key
):
    await edition(database)
    previous = (await matches(client, inspected))["items"][0]
    assert previous["status"] == "matched"
    duplicate = await edition(
        database, title="An unrelated title", identifiers={identifier_key: identifier_value}
    )
    match = (await matches(client, inspected))["items"][0]
    assert match["status"] == "review" and not match["selected_version_id"]
    assert {row["version_id"] for row in match["candidates"] if row["identifier_match"]} >= {
        str(duplicate["version"])
    }
    assert "multiple catalog versions" in match["message"]
    assert (await freeze(client, inspected, previous)).status_code == 409


async def test_match_proof_cannot_select_another_version_or_outlive_group_revision(
    client, admin, database, inspected
):
    chosen = await edition(database)
    other = await edition(
        database, work_id=chosen["work"], identifiers={"isbn_13": "9780306406157"}
    )
    match = (await matches(client, inspected))["items"][0]
    assert match["status"] == "matched"
    forged = await freeze(client, inspected, match, version_id=str(other["version"]))
    assert forged.status_code == 409
    inspection, grouping = inspected
    response = await client.put(
        f"/api/organization/inspections/{inspection['id']}/grouping",
        json={
            "inspection_revision": inspection["snapshot"]["revision"],
            "expected_revision": grouping["revision"],
            "groups": [],
            "excluded": [{"path": "book.epub", "reason": "Not part of this import"}],
        },
    )
    assert response.status_code == 200
    stale = await client.get(
        f"/api/organization/inspections/{inspection['id']}/matches",
        params={"grouping_revision": grouping["revision"]},
    )
    assert stale.status_code == 409
    assert (await freeze(client, inspected, match)).status_code == 409


async def test_canonical_work_keeps_origin_version_in_frozen_match(
    client, admin, database, inspected
):
    chosen = await edition(database)
    async with database() as db, db.begin():
        root = Work(title="Canonical Harbor", authors=["Alex Morgan"])
        db.add(root)
        await db.flush()
        (await db.get(Work, chosen["work"])).redirect_to = root.id
        root_id = root.id
    match = (await matches(client, inspected))["items"][0]
    assert match["status"] == "matched" and match["candidates"][0]["work_id"] == str(root_id)
    response = await freeze(client, inspected, match)
    assert response.status_code == 201, response.text
    assert response.json()["document"]["groups"][0]["work_id"] == str(chosen["work"])


async def test_pagination_and_candidate_limits_are_explicit(client, admin, database, inspected):
    selected = await edition(database)
    async with database() as db, db.begin():
        original = await db.get(ProviderObject, selected["provider"])
        for _ in range(51):
            version = Version(
                work_id=selected["work"], title="First Harbor", medium="ebook", identifiers={}
            )
            db.add(version)
            await db.flush()
            db.add(
                ProviderObject(
                    provider=original.provider,
                    kind="edition",
                    external_id=str(uuid4()),
                    work_id=selected["work"],
                    version_id=version.id,
                    metadata_source_id=original.metadata_source_id,
                    match_status="matched",
                )
            )
    page = await matches(client, inspected)
    match = page["items"][0]
    assert match["truncated"] and match["status"] == "matched" and len(match["candidates"]) == 50
    assert match["selected_version_id"] == str(selected["version"])
    # A second identifier match must remain ambiguous even among many title-only editions.
    await edition(database, work_id=selected["work"])
    assert (await matches(client, inspected))["items"][0]["status"] == "review"
    empty = await matches(client, inspected, offset=1)
    assert empty["total"] == 1 and empty["items"] == []


async def test_inspection_owner_and_source_boundaries(
    client, admin, database, inspected, monkeypatch
):
    inspection, grouping = inspected
    # A different random inspection ID is indistinguishable from an inaccessible one.
    response = await client.get(
        f"/api/organization/inspections/{uuid4()}/matches",
        params={"grouping_revision": grouping["revision"]},
    )
    assert response.status_code == 404
    async with database() as db, db.begin():
        actor = await db.get(User, UUID(admin["id"]))
        other = User(
            username="other-reviewer",
            display_name="Other reviewer",
            role="admin",
            password_hash=actor.password_hash,
        )
        db.add(other)
        await db.flush()
        row = await db.get(DownloadInspection, UUID(inspection["id"]))
        row.owner_id = other.id
    endpoint = f"/api/organization/inspections/{inspection['id']}/matches"
    query = {"grouping_revision": grouping["revision"]}
    assert (await client.get(endpoint, params=query)).status_code == 404
    async with database() as db, db.begin():
        (await db.get(DownloadInspection, UUID(inspection["id"]))).owner_id = UUID(admin["id"])
        (await db.get(User, UUID(admin["id"]))).role = "member"
    assert (await client.get(endpoint, params=query)).status_code == 403
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "admin"
    monkeypatch.setattr(get_settings(), "import_sources", {})
    response = await client.get(
        f"/api/organization/inspections/{inspection['id']}/matches",
        params={"grouping_revision": grouping["revision"]},
    )
    assert response.status_code == 409


async def ready_inspection(client, monkeypatch, root):
    monkeypatch.setattr(get_settings(), "import_sources", {"fixture": root})
    request = await submit(client)
    assert request.status_code == 202, request.text
    await run_worker()
    inspection = (await client.get(f"/api/organization/inspections/{request.json()['id']}")).json()
    assert inspection["state"] == "ready", inspection
    grouping = (
        await client.get(f"/api/organization/inspections/{inspection['id']}/grouping")
    ).json()
    return inspection, grouping


async def test_prefixed_asin_duplicate_prevents_unique_recording_match(
    client, admin, database, tmp_path, monkeypatch
):
    audio(tmp_path / "pack/book.mp3", tags={"asin": "B012345678"})
    inspected = await ready_inspection(client, monkeypatch, tmp_path.resolve())
    chosen = await edition(database, medium="audio", identifiers={"asin": "B012345678"})
    async with database() as db, db.begin():
        (await db.get(Version, chosen["version"])).narrators = ["Jordan Lee"]
    assert (await matches(client, inspected))["items"][0]["status"] == "matched"
    other = await edition(
        database,
        medium="audio",
        title="A different recording",
        identifiers={"ASIN": "\tURN:ASIN:b012345678\n"},
    )
    match = (await matches(client, inspected))["items"][0]
    assert match["status"] == "review" and match["selected_version_id"] is None
    assert {row["version_id"] for row in match["candidates"]} == {
        str(chosen["version"]),
        str(other["version"]),
    }
    assert all(row["identifier_match"] for row in match["candidates"])


async def test_edition_label_still_matches_an_edition_of_the_same_format(
    client, admin, database, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    epub(root / "pack/book.epub", title="First Harbor (Unabridged)", isbn="urn:isbn:1-234-56789-X")
    chosen = await edition(database, title="First Harbor")
    inspection, grouping = await ready_inspection(client, monkeypatch, root)
    report = await matches(client, (inspection, grouping))
    match = report["items"][0]
    assert match["status"] == "matched"
    assert match["selected_version_id"] == str(chosen["version"])
    assert "Embedded title agrees" in match["candidates"][0]["reasons"]
    found = (
        await client.get("/api/catalog/works", params={"q": "First Harbor (Unabridged)"})
    ).json()
    assert [row["id"] for row in found["items"]] == [str(chosen["work"])]


async def test_missing_format_can_be_added_from_the_file_and_imported(
    client, admin, database, tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    audio(root / "pack/book.mp3", title="First Harbor (Unabridged)")
    chosen = await edition(database, title="First Harbor", medium="ebook")
    inspection, grouping = await ready_inspection(client, monkeypatch, root)
    match = (await matches(client, (inspection, grouping)))["items"][0]
    assert match["status"] == "unmatched" and match["candidates"] == []
    assert "add an edition from the file" in match["message"]
    found = (
        await client.get("/api/catalog/works", params={"q": "First Harbor (Unabridged)"})
    ).json()
    assert found["items"][0]["id"] == str(chosen["work"])
    group = grouping["content"]["groups"][0]
    body = {
        "work_id": str(chosen["work"]),
        "group_key": group["key"],
        "grouping_revision": grouping["revision"],
    }
    created = await client.post(
        f"/api/organization/inspections/{inspection['id']}/editions", json=body
    )
    assert created.status_code == 200, created.text
    edition_row = created.json()
    assert edition_row["created"] and edition_row["medium"] == "audio"
    assert edition_row["title"] == "First Harbor"
    again = await client.post(
        f"/api/organization/inspections/{inspection['id']}/editions", json=body
    )
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert again.json()["version_id"] == edition_row["version_id"]
    metadata = (await client.get(f"/api/metadata/works/{chosen['work']}")).json()
    recorded = next(row for row in metadata["versions"] if row["id"] == edition_row["version_id"])
    assert recorded["medium"] == "audio" and recorded["abridged"] is False
    assert recorded["narrators"] == ["Jordan Lee"]
    settings = (await client.get("/api/organization/settings")).json()
    planned = await client.post(
        f"/api/organization/inspections/{inspection['id']}/plans",
        json={
            "inspection_revision": inspection["snapshot"]["revision"],
            "grouping_revision": grouping["revision"],
            "profile_revision": settings["revision"],
            "selections": [
                {
                    "group_key": group["key"],
                    "work_id": edition_row["work_id"],
                    "version_id": edition_row["version_id"],
                    "full_content": True,
                }
            ],
        },
    )
    assert planned.status_code == 201, planned.text
    saved = planned.json()["document"]["groups"][0]
    assert saved["medium"] == "audio" and saved["metadata"]["title"] == "First Harbor"
