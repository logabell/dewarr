import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select, text

from app.db.models import BookList, Integration, LibraryGrant, ListEntry, User, Work
from app.domain.visibility import visible_work
from tests.integration.test_discovery import add_owned, login_member

pytestmark = pytest.mark.integration


async def test_visibility_walks_only_accessible_origins_and_revokes_access(
    client, admin, database, tmp_path
):
    owner_id = UUID(admin["id"])
    reader_id = await login_member(client)
    async with database() as db, db.begin():
        await db.execute(
            insert(Work),
            [
                {
                    "id": uuid4(),
                    "title": f"Unrelated private book {n}",
                    "authors": ["Other writer"],
                    "catalog_public": False,
                    "catalog_owner_id": owner_id,
                }
                for n in range(10000)
            ],
        )
        roots = {
            kind: Work(title=f"Visibility {kind}", catalog_public=kind == "public")
            for kind in ("owned", "shared", "library", "hidden", "public")
        }
        db.add_all(roots.values())
        await db.flush()
        origins = {}
        for kind in ("owned", "shared", "library", "hidden"):
            middle = Work(
                title=f"Intermediate {kind}", catalog_public=False, redirect_to=roots[kind].id
            )
            db.add(middle)
            await db.flush()
            origin = Work(
                title=f"Origin {kind}",
                catalog_public=False,
                catalog_owner_id=reader_id if kind == "owned" else owner_id,
                redirect_to=middle.id,
            )
            db.add(origin)
            await db.flush()
            origins[kind] = origin
        shared = BookList(owner_id=owner_id, name="Shared catalog", shared=True)
        wrong_owner = BookList(
            owner_id=reader_id, name="Cannot share another owner's book", shared=True
        )
        db.add_all([shared, wrong_owner])
        await db.flush()
        db.add_all(
            [
                ListEntry(list_id=shared.id, work_id=origins["shared"].id),
                ListEntry(list_id=wrong_owner.id, work_id=origins["hidden"].id),
            ]
        )
        library = await add_owned(db, origins["library"])
        db.add(LibraryGrant(user_id=reader_id, library_id=library.id))
        root_ids = {kind: row.id for kind, row in roots.items()}
        await db.flush()
        await db.execute(text("ANALYZE works"))
        reader = await db.get(User, reader_id)
        query = select(Work.id).where(Work.redirect_to.is_(None), visible_work(reader))
        assert set(await db.scalars(query)) == {
            root_ids[k] for k in ("owned", "shared", "library", "public")
        }
        compiled = query.compile(dialect=db.bind.dialect, compile_kwargs={"literal_binds": True})
        plan = (
            await db.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled)))
        ).scalar_one()[0]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    metrics = {
        "catalog_works": 10013,
        "recursive_rows": sum(
            n["Actual Rows"] * n["Actual Loops"]
            for n in nodes(plan["Plan"])
            if n["Node Type"] == "Recursive Union"
        ),
        "execution_ms": plan["Execution Time"],
        "plan": plan,
    }
    (tmp_path / "visibility-metrics.json").write_text(json.dumps(metrics, indent=2))
    for kind, root_id in root_ids.items():
        response = await client.get(f"/api/catalog/works/{root_id}")
        assert response.status_code == (404 if kind == "hidden" else 200), response.text
    # Each permission route must be revoked independently, including roots whose
    # only accessible evidence lives two redirects away.
    async with database() as db, db.begin():
        (await db.get(Integration, library.integration_id)).enabled = False
        (await db.get(BookList, shared.id)).shared = False
        (await db.get(Work, origins["owned"].id)).catalog_owner_id = owner_id
    for kind in ("owned", "shared", "library"):
        assert (await client.get(f"/api/catalog/works/{root_ids[kind]}")).status_code == 404
    async with database() as db, db.begin():
        (await db.get(Integration, library.integration_id)).enabled = True
    assert (await client.get(f"/api/catalog/works/{root_ids['library']}")).status_code == 200
    async with database() as db, db.begin():
        await db.execute(delete(LibraryGrant).where(LibraryGrant.user_id == reader_id))
    assert (await client.get(f"/api/catalog/works/{root_ids['library']}")).status_code == 404
    assert metrics["recursive_rows"] < 50, "Permission check traversed unrelated private books"
