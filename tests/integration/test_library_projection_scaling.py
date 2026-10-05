import json
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select, text

from app.db.models import AssetContains, LibraryGrant, Work
from tests.integration.test_discovery import login_member
from tests.integration.test_library_discovery import add

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("surface", ["books", "assets", "groups/authors", "groups/series"])
async def test_library_projection_skips_unrelated_catalog_and_keeps_display_family(
    client, admin, database, tmp_path, surface
):
    async with database() as db, db.begin():
        await db.execute(
            insert(Work),
            [
                {"id": uuid4(), "title": f"Unrelated {n}", "authors": ["Unrelated writer"]}
                for n in range(5000)
            ],
        )
        selected, library, asset = await add(
            db, "Selected title", metadata_snapshot={"series": [{"name": "Selected series"}]}
        )
        # The preferred representative and audio copy live outside the filtered
        # library; seeding must include the full title family, not just one origin.
        representative, audio_library, audio = await add(db, "Selected title", medium="audio")
        selected.redirect_to = representative.id
        selected.authors = representative.authors = ["Selected writer"]
        asset.title = "Source filename"
        db.add(Work(title="Selected title", authors=["Different writer"]))
        await db.flush()
        await db.execute(text("ANALYZE works"))
        ids = library.id, audio_library.id, representative.id
    reader = await login_member(client)
    async with database() as db, db.begin():
        db.add_all([LibraryGrant(user_id=reader, library_id=key) for key in ids[:2]])
    statements = []
    engine = database.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append((statement, parameters))

    params = {"library_id": str(ids[0]), "medium": "ebook"}
    if surface == "assets":
        params["q"] = "Selected title"
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = await client.get(f"/api/library/{surface}", params=params)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1 and len(page["items"]) == 1
    entry = page["items"][0]
    if surface == "books":
        assert entry["id"] == str(ids[2]) and entry["availability"]["audio"]
    elif surface == "assets":
        assert entry["id"] == str(asset.id) and entry["work_ids"] == [str(ids[2])]
    else:
        assert entry["book_count"] == 1
        assert entry["books"][0]["id"] == str(ids[2])
        assert entry["books"][0]["availability"]["audio"]
    plans = []
    async with database() as db:
        connection = await db.connection()
        for statement, parameters in statements:
            plans.append(
                (
                    await connection.exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
                    )
                ).scalar_one()[0]
            )

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    metrics = {
        "surface": surface,
        "works": 5003,
        "execution_ms": sum(plan["Execution Time"] for plan in plans),
        "recursive_rows": sum(
            node["Actual Rows"] * node["Actual Loops"]
            for plan in plans
            for node in nodes(plan["Plan"])
            if node["Node Type"] == "Recursive Union"
        ),
        "plans": plans,
    }
    (tmp_path / "library-projection-metrics.json").write_text(json.dumps(metrics, indent=2))
    async with database() as db, db.begin():
        grant = await db.scalar(
            select(LibraryGrant).where(
                LibraryGrant.user_id == reader, LibraryGrant.library_id == ids[0]
            )
        )
        await db.delete(grant)
    assert (await client.get(f"/api/library/{surface}", params=params)).json()["total"] == 0
    assert metrics["recursive_rows"] < 200, "Small library query walked the unrelated catalog"


@pytest.mark.parametrize(
    "kind", ["missing-confirmed", "scope-unavailable", "partial", "unverified"]
)
async def test_library_seed_keeps_review_copies_without_claiming_ownership(
    client, admin, database, kind
):
    state = kind if kind in {"missing-confirmed", "scope-unavailable"} else "present"
    async with database() as db, db.begin():
        work, library, asset = await add(db, "Review copy", state=state)
        coverage = await db.get(AssetContains, (asset.id, work.id))
        if kind == "partial":
            coverage.part_index, coverage.part_total = 1, 2
        if kind == "unverified":
            coverage.verified = False
    params = {"library_id": str(library.id), "state": state}
    response = await client.get("/api/library/books", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    book = response.json()["items"][0]
    assert book["id"] == str(work.id) and not book["availability"]["owned"]
    groups = await client.get("/api/library/groups/authors", params={"library_id": str(library.id)})
    assert groups.status_code == 200 and groups.json()["total"] == 0
