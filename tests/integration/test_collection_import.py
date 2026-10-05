# ruff: noqa: F811
import json
import time
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select

from app.db.models import ImportEntry, Library, LibraryAsset, Version, Work
from app.domain.inventory import apply_item
from app.importing import execution
from app.jobs.queue import get_queue
from tests.integration.test_acquisition import body
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_execution import ready_route  # noqa: F401
from tests.integration.test_single_file_acquisition import prepare_audio_route
from tests.media_fixtures import audio

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("lock", [False, True])
async def test_collection_revalidation_batches_books_and_rechecks_later_changes(
    database, tmp_path, lock
):
    from app.importing.collection_contents import freeze, verify

    async with database() as db, db.begin():
        books = [Work(title=f"Collected book {n}", authors=["Writer"]) for n in range(100)]
        db.add_all(books)
        await db.flush()
        contents = await freeze(db, [book.id for book in books], uuid4())
        changed_id = books[0].id
    async with database() as db:
        calls = []
        engine = db.bind.sync_engine

        def count(*args):
            calls.append(1)

        event.listen(engine, "before_cursor_execute", count)
        started = time.perf_counter()
        try:
            await verify(db, contents, lock=lock)
        finally:
            elapsed = time.perf_counter() - started
            event.remove(engine, "before_cursor_execute", count)
        metrics = {
            "books": 100,
            "locked": lock,
            "sql_statements": len(calls),
            "elapsed_seconds": elapsed,
        }
        (tmp_path / "collection-metrics.json").write_text(json.dumps(metrics))
        print(metrics)
        assert len(calls) == 1, "Collection revalidation issued per-book reads"
        await db.commit()
        async with database() as other, other.begin():
            (await other.get(Work, changed_id)).title = "Corrected title"
        with pytest.raises(HTTPException, match="Contained book identity changed"):
            await verify(db, contents, lock=lock)


async def start(client, route, key="import-fixture"):
    medium = route["plan"]["document"]["groups"][0]["medium"]
    return await client.post(
        f"/api/organization/plans/{route['plan_id']}/imports",
        headers={"Idempotency-Key": key},
        json={
            "plan_revision": route["plan"]["revision"],
            "destinations": {
                medium: {
                    "id": route["destination"]["id"],
                    "revision": route["destination"]["revision"],
                }
            },
        },
    )


async def collection_plan(client, route):
    books = []
    for name in ("First contained book", "Second contained book"):
        value = await client.post(
            "/api/catalog/works", json={"title": name, "authors": ["Alex Morgan"]}
        )
        books.append(value.json()["id"])
    old = route["plan"]
    inspection = (await client.get(f"/api/organization/inspections/{old['inspection_id']}")).json()
    group = old["document"]["groups"][0]
    settings = (await client.get("/api/organization/settings")).json()
    command = {
        "inspection_revision": inspection["snapshot"]["revision"],
        "profile_revision": settings["revision"],
        "selections": [
            {
                "group_key": inspection["snapshot"]["groups"][0]["key"],
                "work_id": group["work_id"],
                "version_id": group["version_id"],
                "full_content": True,
                "contained_work_ids": books,
                "contents_confirmed": True,
            }
        ],
    }
    response = await client.post(
        f"/api/organization/inspections/{old['inspection_id']}/plans", json=command
    )
    assert response.status_code == 201, response.text
    route["plan"], route["plan_id"] = response.json(), response.json()["id"]
    return books, command


@pytest.mark.parametrize("delayed", [False, True])
@pytest.mark.parametrize("medium", ["ebook", "audio"])
async def test_one_imported_omnibus_confirms_children_only_with_backend_item(
    client, admin, database, ready_route, delayed, medium
):
    route = ready_route
    extension = "epub" if medium == "ebook" else "mp3"
    source = route["source"] / f"pack/book.{extension}"
    if medium == "audio":
        audio(source)
        await prepare_audio_route(
            client, database, route, route["plan"]["document"]["groups"][0]["work_id"], source
        )
    books, _ = await collection_plan(client, route)
    async with database() as db:
        versions = await db.scalar(select(func.count()).select_from(Version))
    original = source.read_bytes()
    route["scan_backend"].detect = not delayed
    result = await start(client, route)
    assert result.status_code == 202, result.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    entry = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()[
        "entries"
    ][0]
    if delayed:
        assert entry["state"] == "awaiting-library", entry
        for work in books:
            assert not (await client.get(f"/api/catalog/works/{work}")).json()["availability"][
                "owned"
            ]
        route["scan_backend"].detect = True
        route["scan_backend"].scan()
        await execution.execute(UUID(entry["operation_id"]))
    entry = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()[
        "entries"
    ][0]
    assert entry["state"] == "confirmed", entry
    for work in books:
        data = (await client.get(f"/api/catalog/works/{work}")).json()
        assert data["availability"]["owned"] and data["availability"]["in_collection"]
        for spec, expected in [
            ({}, "satisfied"),
            ({"standalone": True}, "wanted"),
            ({"language": "en"}, "wanted"),
        ]:
            response = await client.post(
                "/api/requests/preview", json=body({"work": work}, medium, **spec)
            )
            assert response.status_code == 200, response.text
            assert response.json()["targets"][0]["state"] == expected
        if medium == "audio":
            response = await client.post(
                "/api/requests/preview",
                json=body({"work": work}, "audio", required_narrators=["Jordan Lee"]),
            )
            assert (
                response.status_code == 200 and response.json()["targets"][0]["state"] == "wanted"
            )
    async with database() as db, db.begin():
        assert await db.scalar(select(func.count()).select_from(LibraryAsset)) == 1
        assert await db.scalar(select(func.count()).select_from(Version)) == versions
        asset = await db.get(LibraryAsset, UUID(entry["asset_id"]))
        assert str(asset.version_id) == entry["version_id"]
        assert set(asset.containment["work_ids"]) == set(books)
        # Normal inventory must retain the physical edition and all child coverage.
        library = await db.get(Library, asset.library_id)
        from app.adapters.audiobookshelf import ABSItem

        await apply_item(
            db,
            library,
            ABSItem.model_validate(asset.metadata_snapshot),
            library.generation + 1,
            library.integration_id,
            {asset.external_id},
        )
        assert str(asset.version_id) == entry["version_id"]
    viewed = (await client.get("/api/library/assets")).json()["items"]
    assert len(viewed) == 1 and len(viewed[0]["contents"]) == 2
    assert set(book["work_id"] for book in viewed[0]["contents"]) == set(books)
    imported = list(route["target"].rglob(f"*.{extension}"))
    assert len(imported) == 1 and imported[0].stat().st_ino == source.stat().st_ino
    assert imported[0].read_bytes() == original == source.read_bytes()
    assert len(route["scan_backend"].items) == 1
    again = await start(client, route, "repeat-collection-import")
    assert again.json()["entries"][0]["state"] == "skipped", again.text
    await execution.execute(UUID(entry["operation_id"]))
    physical_work = route["plan"]["document"]["groups"][0]["work_id"]
    response = await client.post(
        "/api/requests/preview",
        json=body({"work": physical_work}, medium, **{f"{medium}_version_id": entry["version_id"]}),
    )
    assert response.status_code == 200, response.text
    assert response.json()["targets"][0]["state"] == "satisfied"

    # A changed physical edition invalidates coverage; reverting it is not a fresh review.
    async with database() as db, db.begin():
        asset = await db.get(LibraryAsset, UUID(entry["asset_id"]))
        version = await db.get(Version, UUID(entry["version_id"]))
        original_title = version.title
        library = await db.get(Library, asset.library_id)
        for title in ["Different collection edition", original_title]:
            version.title = title
            await db.flush()
            await apply_item(
                db,
                library,
                ABSItem.model_validate(asset.metadata_snapshot),
                library.generation + 1,
                library.integration_id,
                {asset.external_id},
            )
            assert asset.version_id is None
            assert not asset.containment["valid"]
    for work in books:
        assert not (await client.get(f"/api/catalog/works/{work}")).json()["availability"]["owned"]


@pytest.mark.parametrize("phase", ["before-start", "before-publication", "awaiting-library"])
async def test_changed_child_identity_holds_collection_without_false_ownership(
    client, admin, database, ready_route, phase
):
    route = ready_route
    books, _ = await collection_plan(client, route)
    if phase != "before-start":
        result = await start(client, route)
        assert result.status_code == 202
        entry = result.json()["entries"][0]
        if phase == "awaiting-library":
            route["scan_backend"].detect = False
            await execution.execute(UUID(entry["operation_id"]))
    async with database() as db, db.begin():
        (await db.get(Work, UUID(books[0]))).title = "Corrected identity"
    if phase == "before-start":
        response = await start(client, route)
        assert response.status_code == 409, response.text
        async with database() as db:
            assert not await db.scalar(select(ImportEntry.id))
    else:
        route["scan_backend"].detect = True
        await execution.execute(UUID(entry["operation_id"]))
        value = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()[
            "entries"
        ][0]
        assert value["state"] == "held" and "Contained book identity changed" in value["message"], (
            value
        )
    for work in books:
        assert not (await client.get(f"/api/catalog/works/{work}")).json()["availability"]["owned"]
    assert len(list(route["target"].rglob("*.epub"))) == (1 if phase == "awaiting-library" else 0)


async def test_collection_plan_requires_complete_content_acknowledgment_and_distinct_children(
    client, admin, ready_route
):
    route = ready_route
    books, command = await collection_plan(client, route)
    endpoint = f"/api/organization/inspections/{route['plan']['inspection_id']}/plans"
    command["selections"][0]["contents_confirmed"] = False
    assert (await client.post(endpoint, json=command)).status_code == 422
    command["selections"][0]["contents_confirmed"] = True
    command["selections"][0]["contained_work_ids"] = [books[0], books[0]]
    assert (await client.post(endpoint, json=command)).status_code == 422
    command["selections"][0]["contained_work_ids"] = [books[0], command["selections"][0]["work_id"]]
    assert (await client.post(endpoint, json=command)).status_code == 422
