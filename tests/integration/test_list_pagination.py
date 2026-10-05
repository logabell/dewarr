from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.models import BookList, ListEntry, Work
from tests.integration.test_list_curation import edit, new_list

pytestmark = pytest.mark.integration


async def seed_list(database, identifier, count):
    async with database() as db:
        works = [Work(title=f"Paged book {i:03}", authors=["Page Author"]) for i in range(count)]
        db.add_all(works)
        await db.flush()
        ids = [str(w.id) for w in works]
        db.add_all(
            [
                ListEntry(list_id=UUID(identifier), work_id=w.id, position=i)
                for i, w in enumerate(works)
            ]
        )
        await db.commit()
    return ids


async def test_list_pages_resolve_chains_outside_membership_and_keep_revision(
    client, admin, database
):
    item = await new_list(client)
    other = await new_list(client)
    ids = await seed_list(database, item["id"], 2)
    async with database() as db, db.begin():
        root = Work(title="External canonical root", authors=["Canonical Author"])
        db.add(root)
        await db.flush()
        intermediate = Work(title="Intermediate alias", redirect_to=root.id)
        db.add(intermediate)
        await db.flush()
        for identifier in ids:
            (await db.get(Work, UUID(identifier))).redirect_to = intermediate.id
        root_id = str(root.id)
    path = f"/api/lists/{item['id']}"
    response = await client.get(path)
    assert response.status_code == 200, response.text
    before = response.json()
    assert before["count"] == before["matched"] == 1
    assert [work["id"] for work in before["items"]] == [root_id]
    page = (await client.get("/api/lists/page")).json()
    assert {row["id"]: row["count"] for row in page["items"]} == {item["id"]: 1, other["id"]: 0}
    assert (await client.get(path, params={"q": "canonical"})).json()["matched"] == 1
    assert (await client.get(path, params={"offset": 1})).json()["items"] == []
    # Unrelated membership never changes this list's optimistic concurrency token.
    await seed_list(database, other["id"], 2)
    assert (await client.get(path)).json()["content_revision"] == before["content_revision"]
    async with database() as db, db.begin():
        (await db.get(Work, UUID(ids[0]))).redirect_to = None
    changed = (await client.get(path)).json()
    assert changed["count"] == 2
    assert changed["content_revision"] != before["content_revision"]


async def test_list_index_search_and_pagination_beyond_old_limit(client, admin, database):
    async with database() as db:
        db.add_all(
            [BookList(name=f"Page list {i:03}", owner_id=UUID(admin["id"])) for i in range(215)]
        )
        await db.commit()
    seen = []
    for offset in range(0, 215, 100):
        response = await client.get(
            "/api/lists/page", params={"offset": offset, "limit": 100, "editable": True}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 215 and data["offset"] == offset
        seen.extend(x["id"] for x in data["items"])
    assert len(seen) == len(set(seen)) == 215
    data = (await client.get("/api/lists/page", params={"q": "Page list 214"})).json()
    assert data["total"] == 1 and data["items"][0]["name"] == "Page list 214"
    assert (await client.get("/api/lists/page", params={"q": "%"})).json()["total"] == 0
    assert (await client.get("/api/lists/page", params={"limit": 101})).status_code == 422


async def test_membership_paging_search_catalog_and_cross_page_move(client, admin, database):
    item = await new_list(client)
    ids = await seed_list(database, item["id"], 121)
    path = f"/api/lists/{item['id']}"
    first = (await client.get(path)).json()
    assert first["count"] == first["matched"] == 121 and len(first["items"]) == 50
    second = await client.get(
        path, params={"offset": 50, "expected_revision": first["content_revision"]}
    )
    assert second.status_code == 200, second.text
    assert second.json()["items"][0]["id"] == ids[50]
    final = (await client.get(path, params={"offset": 100})).json()
    assert [w["id"] for w in final["items"]] == ids[100:]
    search = (await client.get(path, params={"q": "book 120"})).json()
    assert search["count"] == 121 and search["matched"] == 1 and search["items"][0]["id"] == ids[-1]
    author = (await client.get(path, params={"q": "Page Author", "offset": 100})).json()
    assert author["matched"] == 121
    catalog = (await client.get(path + "/catalog", params={"q": "book 120"})).json()
    assert catalog["member_ids"] == [ids[-1]]
    move = await client.post(
        path + "/move",
        json={"work_id": ids[50], "direction": -1, "expected_revision": first["content_revision"]},
    )
    assert move.status_code == 204, move.text
    assert (
        await client.get(
            path, params={"offset": 50, "expected_revision": first["content_revision"]}
        )
    ).status_code == 409
    order = (await client.get(path, params={"offset": 49, "limit": 2})).json()
    assert [w["id"] for w in order["items"]] == [ids[50], ids[49]]
    assert (
        await edit(
            client, item["id"], "remove", [ids[0]], expected_revision=first["content_revision"]
        )
    ).status_code == 409
    assert (await client.get(path, params={"limit": 101})).status_code == 422


async def test_move_with_tied_positions_and_boundary_validation(client, admin, database):
    # Real aliases are covered by the merge corpus; tied positions must not make moves no-ops.
    item = await new_list(client)
    await seed_list(database, item["id"], 3)
    async with database() as db:
        entries = (
            await db.scalars(select(ListEntry).where(ListEntry.list_id == UUID(item["id"])))
        ).all()
        for entry in entries:
            entry.position = 0
        await db.commit()
    path = f"/api/lists/{item['id']}"
    before = (await client.get(path)).json()
    ordered = [w["id"] for w in before["items"]]
    result = await client.post(
        path + "/move",
        json={
            "work_id": ordered[1],
            "direction": -1,
            "expected_revision": before["content_revision"],
        },
    )
    assert result.status_code == 204, result.text
    after = (await client.get(path)).json()
    assert [w["id"] for w in after["items"]] == [ordered[1], ordered[0], ordered[2]]
    assert (
        await client.post(
            path + "/move",
            json={
                "work_id": ordered[1],
                "direction": -1,
                "expected_revision": after["content_revision"],
            },
        )
    ).status_code == 409
    assert (await client.get(f"/api/lists/{uuid4()}/catalog")).status_code == 404


async def test_preview_rejects_selection_from_old_membership_episode(client, admin, database):
    item = await new_list(client)
    ids = await seed_list(database, item["id"], 1)
    path = f"/api/lists/{item['id']}"
    revision = (await client.get(path)).json()["content_revision"]
    assert (await edit(client, item["id"], "remove", ids)).status_code == 200
    assert (await edit(client, item["id"], "add", ids)).status_code == 200
    for route, body in [
        ("requests/preview", {"work_ids": ids, "specification": {"mode": "ebook"}}),
        ("acquisition/preview", {"mode": "browse", "specification": {"mode": "ebook"}}),
    ]:
        response = await client.post(
            path + "/" + route,
            json={**body, "expected_content_revision": revision},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 409, response.text
        assert "membership or order changed" in response.json()["detail"]


async def test_paged_move_and_catalog_hide_inaccessible_memberships(client, admin, database):
    from tests.integration.test_discovery import login_member

    await login_member(client)
    item = await new_list(client)
    ids = await seed_list(database, item["id"], 2)
    async with database() as db, db.begin():
        hidden = Work(
            title="Private hidden selection",
            catalog_public=False,
            catalog_owner_id=UUID(admin["id"]),
        )
        db.add(hidden)
        await db.flush()
        entries = (
            await db.scalars(
                select(ListEntry)
                .where(ListEntry.list_id == UUID(item["id"]))
                .order_by(ListEntry.position)
            )
        ).all()
        entries[0].position, entries[1].position = 0, 2
        db.add(ListEntry(list_id=UUID(item["id"]), work_id=hidden.id, position=1))
        hidden_id = hidden.id
    path = f"/api/lists/{item['id']}"
    before = (await client.get(path, params={"limit": 1})).json()
    assert before["count"] == 2 and len(before["items"]) == 1
    response = await client.post(
        path + "/move",
        json={"work_id": ids[1], "direction": -1, "expected_revision": before["content_revision"]},
    )
    assert response.status_code == 204, response.text
    after = (await client.get(path)).json()
    assert [w["id"] for w in after["items"]] == list(reversed(ids))
    assert str(hidden_id) not in str(after)
    assert (await client.get(path + "/catalog", params={"q": "Private hidden"})).json()[
        "total"
    ] == 0
    async with database() as db:
        actual = list(
            (
                await db.scalars(
                    select(ListEntry)
                    .where(ListEntry.list_id == UUID(item["id"]))
                    .order_by(ListEntry.position)
                )
            ).all()
        )
        assert [str(e.work_id) for e in actual] == [ids[1], str(hidden_id), ids[0]]


async def test_detail_pages_beyond_ten_thousand_without_hydrating_all_books(
    client, admin, database, monkeypatch
):
    from app.api import lists

    item = await new_list(client)
    ids = await seed_list(database, item["id"], 10005)
    observed = []
    original = lists.availability_for

    async def bounded(db, user, work_ids):
        observed.append(len(work_ids))
        return await original(db, user, work_ids)

    monkeypatch.setattr(lists, "availability_for", bounded)
    response = await client.get(f"/api/lists/{item['id']}", params={"offset": 10000, "limit": 25})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["count"] == data["matched"] == 10005
    assert [w["id"] for w in data["items"]] == ids[-5:]
    assert observed == [5]


async def test_index_page_counts_only_authorized_lists_and_editable_destinations(
    client, admin, database
):
    from tests.integration.test_discovery import login_member

    private = await new_list(client)
    shared = await new_list(client)
    await client.patch(f"/api/lists/{shared['id']}", json={"shared": True})
    await login_member(client)
    own = await new_list(client)
    page = (await client.get("/api/lists/page")).json()
    assert page["total"] == 2
    assert {x["id"] for x in page["items"]} == {own["id"], shared["id"]}
    assert private["id"] not in str(page)
    editable = (await client.get("/api/lists/page", params={"editable": True})).json()
    assert editable["total"] == 1 and editable["items"][0]["id"] == own["id"]
    await client.post("/api/auth/logout")
    assert (await client.get("/api/lists/page")).status_code == 401


async def test_relative_move_preserves_canonical_alias_membership(client, admin, database):
    item = await new_list(client)
    ids = await seed_list(database, item["id"], 3)
    async with database() as db, db.begin():
        alias = Work(title="Alias title", redirect_to=UUID(ids[1]))
        db.add(alias)
        await db.flush()
        db.add(ListEntry(list_id=UUID(item["id"]), work_id=alias.id, position=5))
        alias_id = alias.id
    path = f"/api/lists/{item['id']}"
    before = (await client.get(path, params={"limit": 1})).json()
    assert before["count"] == 3
    command = {"work_id": ids[1], "direction": -1, "expected_revision": before["content_revision"]}
    assert (await client.post(path + "/move", json=command)).status_code == 204
    assert (await client.post(path + "/move", json=command)).status_code == 409
    after = (await client.get(path)).json()
    assert [w["id"] for w in after["items"]] == [ids[1], ids[0], ids[2]]
    async with database() as db:
        entries = list(
            (await db.scalars(select(ListEntry).where(ListEntry.list_id == UUID(item["id"])))).all()
        )
        positions = {e.work_id: e.position for e in entries}
        assert positions[UUID(ids[1])] == positions[alias_id]
