from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, event, func, select, update

from app.db.models import (
    AcquisitionIntent,
    AssetContains,
    BookList,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    ListEntry,
    ListSubscription,
    Operation,
    Work,
)
from app.db.session import get_engine
from tests.integration.test_discovery import add_owned, login_member

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 19, tzinfo=UTC)


async def followed(db, owner, name="Followed books", provider="hardcover", days=0, **values):
    item = BookList(owner_id=owner, name=name)
    db.add(item)
    await db.flush()
    subscription = ListSubscription(
        list_id=item.id,
        provider=provider,
        encrypted_config="private-feed-key-must-not-be-read",
        created_at=NOW - timedelta(days=days),
        **values,
    )
    db.add(subscription)
    await db.flush()
    return item, subscription


async def books(db, item, count):
    works = [
        Work(title=f"Book {i:04}", authors=["List writer"], provisional=False) for i in range(count)
    ]
    db.add_all(works)
    await db.flush()
    db.add_all([ListEntry(list_id=item.id, work_id=w.id, position=i) for i, w in enumerate(works)])
    await db.flush()
    return works


async def shelf(client, **params):
    response = await client.get("/api/discovery/followed-lists", params=params)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    assert "private-feed-key" not in response.text
    return response.json()


async def test_followed_lists_group_aliases_preview_order_and_count_scoped_holdings(
    client, admin, database
):
    async with database() as db, db.begin():
        item, subscription = await followed(db, UUID(admin["id"]), last_success_at=NOW)
        works = await books(db, item, 6)
        works[1].redirect_to = works[0].id
        works[2].provisional = True
        library = await add_owned(db, works[1])
        asset = await db.scalar(select(LibraryAsset).where(LibraryAsset.library_id == library.id))
        asset.state = "stale"
        asset.containment = {"kind": "reviewed"}
        db.add(AssetContains(asset_id=asset.id, work_id=works[3].id, verified=True))
        another = await add_owned(db, works[0])
        other_asset = await db.scalar(
            select(LibraryAsset).where(LibraryAsset.library_id == another.id)
        )
        other_asset.medium = "audio"
        ids = [str(works[i].id) for i in (0, 2, 3)]
    result = await shelf(client)
    card = result["items"][0]
    assert result["total"] == 1 and card["count"] == 5 and card["owned"] == 2
    assert card["provisional"] == 1
    assert card["inventory_stale"]
    assert [w["id"] for w in card["books"]] == ids
    assert card["books"][0]["availability"] == {
        "owned": True,
        "ebook": True,
        "audio": True,
        "stale": True,
        "in_collection": True,
        "ebook_versions": 1,
        "audio_versions": 1,
        "primary_audio_narrators": [],
        "primary_audio_version_id": None,
        "primary_ebook_version_id": None,
        "ebook_stale": True,
        "audio_stale": False,
        "parts_owned": 0,
        "parts_total": 0,
        "parts_medium": None,
    }
    assert card["books"][2]["availability"]["in_collection"]
    assert datetime.fromisoformat(card["last_success_at"]) == NOW


async def test_provider_filter_pagination_paused_empty_and_detached(client, admin, database):
    async with database() as db, db.begin():
        owner = UUID(admin["id"])
        older, _ = await followed(db, owner, "Older Hardcover", days=2)
        goodreads, paused = await followed(
            db, owner, "Paused Goodreads", "goodreads", days=1, enabled=False
        )
        newest, newest_sub = await followed(db, owner, "Newest failed", state="failed")
        db.add(BookList(owner_id=owner, name="Local only"))
        ids = [str(newest.id), str(goodreads.id), str(older.id)]
        removed_id = newest_sub.id
    seen = [(await shelf(client, offset=i, limit=1))["items"][0]["id"] for i in range(3)]
    assert seen == ids
    data = await shelf(client, provider="goodreads", limit=1)
    assert data["total"] == 1 and data["items"][0]["id"] == ids[1]
    assert not data["items"][0]["enabled"] and data["items"][0]["count"] == 0
    assert data["items"][0]["books"] == [] and data["items"][0]["last_success_at"] is None
    assert (await shelf(client, provider="hardcover", offset=1, limit=1))["items"][0]["id"] == ids[
        2
    ]
    assert (await shelf(client, offset=9))["items"] == []
    async with database() as db, db.begin():
        await db.execute(delete(ListSubscription).where(ListSubscription.id == removed_id))
    assert (await shelf(client))["total"] == 2


@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_owner_scope_private_titles_grants_and_revocation(client, admin, database, role):
    async with database() as db, db.begin():
        shared, _ = await followed(db, UUID(admin["id"]), "Other owner's shared subscription")
        shared.shared = True
        private = Work(
            title="Hidden metadata", catalog_public=False, catalog_owner_id=UUID(admin["id"])
        )
        db.add(private)
        await db.flush()
        private_id = private.id
    owner = await login_member(client, role)
    assert (await shelf(client))["total"] == 0
    async with database() as db, db.begin():
        own, _ = await followed(db, owner)
        works = await books(db, own, 2)
        db.add(ListEntry(list_id=own.id, work_id=private_id, position=-1))
        library = await add_owned(db, works[0])
        library_id, integration_id = library.id, library.integration_id
    card = (await shelf(client))["items"][0]
    assert card["count"] == 2 and card["owned"] == 0 and "Hidden metadata" not in str(card)
    async with database() as db, db.begin():
        db.add(LibraryGrant(user_id=owner, library_id=library_id))
    assert (await shelf(client))["items"][0]["owned"] == 1
    async with database() as db, db.begin():
        await db.execute(update(Library).where(Library.id == library_id).values(accessible=False))
    assert (await shelf(client))["items"][0]["owned"] == 0
    async with database() as db, db.begin():
        await db.execute(update(Library).where(Library.id == library_id).values(accessible=True))
        await db.execute(
            update(Integration).where(Integration.id == integration_id).values(enabled=False)
        )
    assert (await shelf(client))["items"][0]["owned"] == 0
    async with database() as db, db.begin():
        await db.execute(
            update(Integration).where(Integration.id == integration_id).values(enabled=True)
        )
        await db.execute(delete(LibraryGrant))
    assert (await shelf(client))["items"][0]["owned"] == 0


async def test_incomplete_missing_or_unverified_assets_do_not_count(client, admin, database):
    async with database() as db, db.begin():
        item, _ = await followed(db, UUID(admin["id"]))
        works = await books(db, item, 3)
        for index, work in enumerate(works):
            library = await add_owned(db, work)
            asset = await db.scalar(
                select(LibraryAsset).where(LibraryAsset.library_id == library.id)
            )
            if index == 0:
                asset.full_content = False
            elif index == 1:
                asset.state = "missing-suspected"
            else:
                await db.execute(
                    update(AssetContains)
                    .where(AssetContains.asset_id == asset.id)
                    .values(verified=False)
                )
    card = (await shelf(client))["items"][0]
    assert card["owned"] == 0 and all(not w["availability"]["owned"] for w in card["books"])


async def test_stale_holdings_outside_preview_still_warn(client, admin, database):
    async with database() as db, db.begin():
        item, _ = await followed(db, UUID(admin["id"]))
        works = await books(db, item, 4)
        library = await add_owned(db, works[-1])
        await db.execute(
            update(LibraryAsset).where(LibraryAsset.library_id == library.id).values(state="stale")
        )
    card = (await shelf(client))["items"][0]
    assert card["owned"] == 1 and card["inventory_stale"]
    assert len(card["books"]) == 3
    assert all(not work["availability"]["stale"] for work in card["books"])


async def test_batched_preview_is_bounded_and_browsing_has_no_effects(
    client, admin, database, monkeypatch
):
    from app.api import list_discovery

    async with database() as db, db.begin():
        for i in range(5):
            item, _ = await followed(db, UUID(admin["id"]), f"List {i}")
            await books(db, item, 251)
    hydrated = []
    original = list_discovery.availability_for

    async def observe(db, user, ids):
        hydrated.append(len(ids))
        return await original(db, user, ids)

    monkeypatch.setattr(list_discovery, "availability_for", observe)
    counts = []
    for limit in (1, 4):
        reads = []

        def record(conn, cursor, statement, parameters, context, executemany, reads=reads):
            reads.append(statement)

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            result = await shelf(client, limit=limit)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        counts.append(len(reads))
        assert result["total"] == 5
        assert all(item["count"] == 251 and len(item["books"]) == 3 for item in result["items"])
        assert not any(
            sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in reads
        )
    assert counts[0] == counts[1] and hydrated == [3, 12]
    async with database() as db:
        for model in (Operation, AcquisitionIntent):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize(
    "params", [{"provider": "unknown"}, {"offset": -1}, {"limit": 0}, {"limit": 13}]
)
async def test_bounds(client, admin, params):
    assert (await client.get("/api/discovery/followed-lists", params=params)).status_code == 422


async def test_authentication(client):
    assert (await client.get("/api/discovery/followed-lists")).status_code == 401
