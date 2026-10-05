"""Identity lookups should cost the size of one family, not the whole catalog."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text

from app.db.models import Work
from app.domain.work_graph import family_ids

pytestmark = pytest.mark.integration


async def test_family_lookup_ignores_unrelated_catalog_and_tracks_undo(database, tmp_path):
    root, branch, leaf, sibling = [uuid4() for _ in range(4)]
    async with database() as db, db.begin():
        await db.execute(
            insert(Work),
            [{"id": uuid4(), "title": f"Unrelated {index}"} for index in range(5000)]
            + [
                {"id": identifier, "title": "Family"}
                for identifier in (root, branch, leaf, sibling)
            ],
        )
        for identifier, parent in ((branch, root), (leaf, branch), (sibling, root)):
            (await db.get(Work, identifier)).redirect_to = parent
        await db.flush()
        await db.execute(text("ANALYZE works"))
        statement = family_ids(leaf)
        assert set(await db.scalars(statement)) == {root, branch, leaf, sibling}
        sql = str(
            statement.compile(dialect=db.bind.dialect, compile_kwargs={"literal_binds": True})
        )
        plan = (await db.scalar(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)))[0]

        def visited(node):
            return node["Actual Rows"] * node["Actual Loops"] + sum(
                visited(child) for child in node.get("Plans", [])
            )

        metrics = {
            "catalog_works": 5004,
            "plan_rows": visited(plan["Plan"]),
            "execution_ms": plan["Execution Time"],
            "plan": plan,
        }
        (tmp_path / "family-metrics.json").write_text(json.dumps(metrics, indent=2))
        print(f"Family lookup: {metrics['plan_rows']} plan rows, {metrics['execution_ms']} ms")
        assert metrics["plan_rows"] < 100, "One family's lookup traversed unrelated books"
        # A later lookup must see graph edits in the same transaction.
        (await db.get(Work, branch)).redirect_to = None
        await db.flush()
        assert set(await db.scalars(family_ids(leaf))) == {branch, leaf}
        assert set(await db.scalars(family_ids(root))) == {root, sibling}
        assert not list(await db.scalars(family_ids(uuid4())))
        assert set(await db.scalars(select(Work.id).where(Work.id.in_(family_ids(root))))) == {
            root,
            sibling,
        }
        assert set(await db.scalars(family_ids(str(root)))) == {root, sibling}
        # Malformed legacy relationships must neither loop nor contaminate an
        # unrelated request. A cycle has no canonical root and yields no family.
        (await db.get(Work, branch)).redirect_to = leaf
        await db.flush()
        await db.execute(text("SET LOCAL statement_timeout = '1s'"))
        assert not list(await db.scalars(family_ids(leaf)))
        assert set(await db.scalars(family_ids(root))) == {root, sibling}
