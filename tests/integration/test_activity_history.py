from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update

from app.db.models import BookList, Operation, Work
from tests.integration.test_discovery import login_member

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 19, tzinfo=UTC)


def operation(owner, number=0, **values):
    return Operation(
        owner_id=owner,
        idempotency_key=str(uuid4()),
        kind=values.pop("kind", "system.probe"),
        status=values.pop("status", "completed"),
        message=values.pop("message", f"History task {number:03}"),
        created_at=values.pop("created_at", NOW - timedelta(minutes=number)),
        **values,
    )


async def history(client, **params):
    response = await client.get("/api/activity/page", params=params)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    return response.json()


async def test_history_pages_beyond_recent_hundred_and_filters_before_count(
    client, admin, database
):
    async with database() as db, db.begin():
        rows = [operation(UUID(admin["id"]), i) for i in range(131)]
        rows[-1].status = "failed"
        rows[-1].kind = "metadata.enrich"
        rows[-1].message = "Older metadata observation failed"
        db.add_all(rows)
        await db.flush()
        ids = [str(row.id) for row in rows]
    all_ids = []
    for offset in range(0, 131, 25):
        data = await history(client, offset=offset)
        assert data["total"] == 131 and data["offset"] == offset
        all_ids.extend(item["id"] for item in data["items"])
    assert all_ids == ids
    result = await history(client, status="failed", kind="metadata.enrich", q="METADATA")
    assert result["total"] == 1 and result["items"][0]["id"] == ids[-1]
    assert result["statuses"] == ["completed", "failed"]
    assert result["kinds"] == ["metadata.enrich", "system.probe"]
    assert (await history(client, q=ids[-1]))["items"][0]["id"] == ids[-1]
    assert (await history(client, offset=1000))["items"] == []
    legacy = await client.get("/api/activity")
    assert legacy.status_code == 200 and len(legacy.json()) == 100
    assert "context" not in legacy.json()[0]


async def test_literal_search_ignores_private_payloads_and_does_not_mutate(client, admin, database):
    async with database() as db, db.begin():
        rows = [
            operation(UUID(admin["id"]), message="100% complete_under\\root"),
            operation(
                UUID(admin["id"]),
                message="100x completeXunderXroot",
                payload={"secret": "hidden-payload-token", "href": "https://example.invalid"},
            ),
        ]
        db.add_all(rows)
        await db.flush()
        ids = {row.id for row in rows}
    for query in ("%", "_", "\\"):
        assert (await history(client, q=query))["total"] == 1
    assert (await history(client, q="hidden-payload-token"))["total"] == 0
    data = await history(client)
    assert "hidden-payload-token" not in str(data) and "example.invalid" not in str(data)
    assert all(row["context"] is None for row in data["items"])
    async with database() as db:
        assert set(await db.scalars(select(Operation.id))) == ids
        assert await db.scalar(select(func.count()).select_from(Operation)) == 2


@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_rows_and_filter_options_are_owner_scoped(client, admin, database, role):
    async with database() as db, db.begin():
        db.add(
            operation(
                UUID(admin["id"]),
                kind="private.kind",
                status="private-state",
                message="Hidden operation",
            )
        )
    owner = await login_member(client, role)
    assert (await history(client))["total"] == 0
    async with database() as db, db.begin():
        db.add(operation(owner, message="Own operation"))
    data = await history(client)
    assert (
        data["total"] == 1
        and data["statuses"] == ["completed"]
        and data["kinds"] == ["system.probe"]
    )
    assert "Hidden operation" not in str(data) and "private-state" not in str(data)


async def test_contexts_use_current_visible_canonical_records_and_fail_closed(
    client, admin, database
):
    async with database() as db, db.begin():
        shared = BookList(owner_id=UUID(admin["id"]), name="Shared reading", shared=True)
        private = BookList(owner_id=UUID(admin["id"]), name="Secret list")
        work = Work(title="Current book", catalog_public=True)
        hidden = Work(title="Hidden book", catalog_public=False, catalog_owner_id=UUID(admin["id"]))
        db.add_all([shared, private, work, hidden])
        await db.flush()
        alias = Work(title="Old book name", redirect_to=work.id)
        db.add(alias)
        await db.flush()
        shared_id, private_id, work_id, alias_id, hidden_id = (
            shared.id,
            private.id,
            work.id,
            alias.id,
            hidden.id,
        )
    owner = await login_member(client)
    async with database() as db, db.begin():
        db.add_all(
            [
                operation(owner, 1, kind="lists.sync", payload={"list_id": str(shared_id)}),
                operation(
                    owner,
                    2,
                    kind="lists.requests",
                    payload={"command": {"list_id": str(private_id)}},
                ),
                operation(
                    owner, 3, kind="sources.search", payload={"command": {"work_id": str(alias_id)}}
                ),
                operation(owner, 4, kind="metadata.enrich", payload={"work_id": str(hidden_id)}),
                operation(
                    owner, 5, kind="lists.csv", payload={"list_id": "https://example.invalid"}
                ),
                operation(owner, 6, kind="lists.requests", payload={"command": "malformed"}),
                operation(owner, 7, kind="library.sync"),
                operation(owner, 8, kind="unknown.kind", payload={"work_id": str(work_id)}),
                operation(owner, 9, kind="lists.sync", payload={"list_id": str(uuid4())}),
            ]
        )
    data = await history(client)
    assert data["items"][0]["context"] == {
        "href": f"/discover?view=yours&list={shared_id}",
        "label": "Open list: Shared reading",
    }
    assert data["items"][2]["context"] == {
        "href": f"/books/{work_id}?tab=sources",
        "label": "Open book: Current book",
    }
    assert all(item["context"] is None for i, item in enumerate(data["items"]) if i not in (0, 2))
    assert "Hidden book" not in str(data) and "Secret list" not in str(data)
    async with database() as db, db.begin():
        await db.execute(update(BookList).where(BookList.id == shared_id).values(shared=False))
        await db.execute(
            update(Work)
            .where(Work.id.in_([work_id, alias_id]))
            .values(catalog_public=False, catalog_owner_id=UUID(admin["id"]))
        )
    assert all(item["context"] is None for item in (await history(client))["items"])


async def test_supported_list_context_contracts_and_deleted_lists(client, admin, database):
    async with database() as db, db.begin():
        item = BookList(owner_id=UUID(admin["id"]), name="Current list title")
        db.add(item)
        await db.flush()
        list_id = item.id
        for kind, payload in [
            ("lists.writeback", {"list_id": str(list_id)}),
            ("lists.writeback.compare", {"binding": {"list_id": str(list_id)}}),
            ("lists.curate", {"command": {"list_id": str(list_id)}}),
            ("discovery.follow-list", {"result": {"list_id": str(list_id)}}),
        ]:
            db.add(operation(UUID(admin["id"]), kind=kind, payload=payload))
    data = await history(client)
    assert all(row["context"]["label"] == "Open list: Current list title" for row in data["items"])
    ids = [row["id"] for row in data["items"]]
    assert ids == sorted(ids)
    async with database() as db, db.begin():
        await db.execute(delete(BookList).where(BookList.id == list_id))
    assert all(row["context"] is None for row in (await history(client))["items"])


async def test_history_only_hydrates_summary_and_known_context_fields(
    client, admin, database, monkeypatch
):
    from app.api import operations

    async with database() as db, db.begin():
        db.add(
            operation(
                UUID(admin["id"]),
                payload={"manifest": "x" * 100000, "secret": "private-command-secret"},
            )
        )
    original = operations.activity_contexts
    seen = []

    async def inspect(db, user, rows):
        seen.extend(rows)
        assert all(
            not hasattr(row, "payload") and not hasattr(row, "idempotency_key") for row in rows
        )
        return await original(db, user, rows)

    monkeypatch.setattr(operations, "activity_contexts", inspect)
    result = await history(client)
    assert len(seen) == 1 and "private-command-secret" not in str(result)


@pytest.mark.parametrize(
    "params",
    [
        {"offset": -1},
        {"limit": 101},
        {"limit": 0},
        {"q": "a" * 301},
        {"status": "a" * 41},
        {"kind": "a" * 61},
    ],
)
async def test_bounds(client, admin, params):
    assert (await client.get("/api/activity/page", params=params)).status_code == 422


async def test_anonymous(client):
    assert (await client.get("/api/activity/page")).status_code == 401
