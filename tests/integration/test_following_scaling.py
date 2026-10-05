import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select, text

from app.db.models import (
    AssetContains,
    BookList,
    LibraryAsset,
    LibraryGrant,
    ListObservation,
    ListSubscription,
    Work,
)
from app.security import encrypt_secrets
from tests.integration.test_discovery import add_owned, login_member

pytestmark = pytest.mark.integration


async def test_following_projects_only_selected_families_with_sibling_holdings(
    client, admin, database, tmp_path
):
    now = datetime.now(UTC)
    reader_id = await login_member(client)
    async with database() as db, db.begin():
        item = BookList(owner_id=reader_id, name="Followed author")
        db.add(item)
        await db.flush()
        subscription = ListSubscription(
            list_id=item.id,
            provider="hardcover",
            source_kind="author",
            encrypted_config=encrypt_secrets({"external_id": "42", "complete": True}),
            last_success_at=now,
        )
        root = Work(title="Selected root", catalog_public=False, catalog_owner_id=reader_id)
        db.add_all([subscription, root])
        await db.flush()
        observed = Work(title="Observed origin", redirect_to=root.id)
        sibling = Work(title="Library origin outside followed catalog", redirect_to=root.id)
        db.add_all([observed, sibling])
        await db.flush()
        db.add(
            ListObservation(
                subscription_id=subscription.id,
                external_id="10",
                work_id=observed.id,
                last_seen_at=now,
                snapshot={
                    "title": "Selected book",
                    "authors": ["Writer"],
                    "release_date": "2000-01-01",
                },
            )
        )
        library = await add_owned(db, sibling)
        db.add(LibraryGrant(user_id=reader_id, library_id=library.id))
        # A large accessible library must not be aggregated for one followed book.
        unrelated = [uuid4() for _ in range(20000)]
        assets = [uuid4() for _ in unrelated]
        await db.execute(
            insert(Work),
            [{"id": key, "title": f"Unrelated {n}"} for n, key in enumerate(unrelated)],
        )
        await db.execute(
            insert(LibraryAsset),
            [
                {
                    "id": asset_id,
                    "library_id": library.id,
                    "external_id": f"other-{n}",
                    "medium": "ebook",
                    "state": "present",
                    "full_content": True,
                }
                for n, asset_id in enumerate(assets)
            ],
        )
        await db.execute(
            insert(AssetContains),
            [
                {"asset_id": asset_id, "work_id": work_id, "verified": True}
                for asset_id, work_id in zip(assets, unrelated, strict=True)
            ],
        )
        await db.execute(text("ANALYZE works"))
        await db.execute(text("ANALYZE asset_contains"))
        await db.execute(text("ANALYZE library_assets"))
        for table in (
            "library_grants",
            "libraries",
            "integrations",
            "list_observations",
            "list_subscriptions",
        ):
            await db.execute(text(f"ANALYZE {table}"))

    statements = []
    engine = database.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = await client.get("/api/following/overview")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    summary = response.json()["items"][0]
    assert summary["total_books"] == summary["library_books"] == 1
    assert summary["missing_books"] == 0
    assert summary["latest_books"][0]["work_id"] == str(root.id)
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
        "works": len(unrelated) + 3,
        "assets": len(assets) + 1,
        "recursive_rows": sum(
            node["Actual Rows"] * node["Actual Loops"]
            for plan in plans
            for node in nodes(plan["Plan"])
            if node["Node Type"] == "Recursive Union"
        ),
        "execution_ms": sum(plan["Execution Time"] for plan in plans),
        "coverage_rows": sum(
            node["Actual Rows"] * node["Actual Loops"]
            for plan in plans
            for node in nodes(plan["Plan"])
            if node.get("Relation Name") == "asset_contains"
        ),
        "plans": plans,
    }
    (tmp_path / "following-metrics.json").write_text(json.dumps(metrics, indent=2))
    owned = (
        await client.get(f"/api/following/{item.id}/books", params={"filter": "library"})
    ).json()
    assert owned["total"] == 1 and owned["items"][0]["ebook"]
    # Revoking the sibling library grant must remove ownership, even though the
    # observed book remains in the reader's saved catalog.
    async with database() as db, db.begin():
        grant = await db.scalar(select(LibraryGrant).where(LibraryGrant.user_id == reader_id))
        await db.delete(grant)
    missing = (
        await client.get(f"/api/following/{item.id}/books", params={"filter": "missing"})
    ).json()
    assert missing["total"] == 1 and not missing["items"][0]["ebook"]
    assert metrics["recursive_rows"] < 50, "Following summary traversed unrelated books"
    assert metrics["coverage_rows"] < 100, "Following summary scanned unrelated library coverage"
