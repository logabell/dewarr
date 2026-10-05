import asyncio
import json
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, func, select, update

from app.adapters.hardcover_community import (
    COMMUNITY_BOOKS,
    COMMUNITY_LIST,
    COMMUNITY_LISTS,
    COMMUNITY_MATCHES,
    COMMUNITY_SEARCH,
)
from app.adapters.hardcover_lists import PAGE
from app.db.models import (
    AcquisitionIntent,
    BookList,
    CatalogAccount,
    ListAcquisitionPolicy,
    ListEntry,
    ListSubscription,
    Operation,
    ProviderCache,
    User,
)
from app.domain.catalog_network import CatalogGateway
from app.jobs.queue import get_queue
from tests.integration.test_discovery import add_owned, add_work, login_member
from tests.integration.test_metadata import connect
from tests.unit.test_hardcover_community import books, row

pytestmark = pytest.mark.integration


@pytest.fixture
def provider(monkeypatch):
    from app.api import metadata
    from app.domain import hardcover_subscriptions

    state = {
        "calls": [],
        "hook": None,
        "public": True,
        "count": 2,
        "failure": None,
        "keys": [42, 43],
        "records": {},
    }

    async def respond(request):
        body = json.loads(request.content)
        state["calls"].append(body)
        if state["hook"]:
            hook, state["hook"] = state["hook"], None
            await hook()
        if state["failure"]:
            return httpx.Response(state["failure"], json={})
        query, variables = body["query"], body["variables"]
        info = row(
            books_count=state["count"],
            list_books=[{"id": i, "book_id": key} for i, key in enumerate(state["keys"], 1)],
        )
        if query in {COMMUNITY_LISTS, COMMUNITY_LIST, COMMUNITY_MATCHES}:
            value = {"lists": [info] if state["public"] else []}
        elif query == COMMUNITY_BOOKS:
            value = books(*variables["ids"])
            value["books"] = [state["records"].get(r["id"], r) for r in value["books"]]
        elif query == COMMUNITY_SEARCH:
            value = {"search": {"results": {"hits": [{"document": {"id": 91}}], "found": 1}}}
        elif query == PAGE:
            entries = [
                {
                    "id": i,
                    "book_id": key,
                    "edition_id": None,
                    "position": i,
                    "date_added": None,
                    "book": state["records"].get(
                        key, {"id": key, "title": f"Book {key}", "cached_contributors": []}
                    ),
                }
                for i, key in enumerate(state["keys"], 1)
                if i > variables["after"]
            ]
            value = {"lists": [{**info, "user_id": 7, "list_books": entries}]}
        else:
            raise AssertionError(query)
        return httpx.Response(200, json={"data": value})

    class Gateway(CatalogGateway):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(respond))

        async def reserve(self):
            pass

    monkeypatch.setattr(metadata, "CatalogGateway", Gateway)
    monkeypatch.setattr(hardcover_subscriptions, "CatalogGateway", Gateway)
    return state


async def follow(client, key="community-follow-1", **body):
    return await client.post(
        "/api/discovery/lists/91/follow", json=body, headers={"Idempotency-Key": key}
    )


async def test_preview_follow_and_worker_sync_use_shared_services_without_acquisition(
    client, admin, database, provider
):
    await connect(client)
    async with database() as db, db.begin():
        work = await add_work(db, "42")
        await add_owned(db, work)
    browse = await client.get("/api/discovery/lists", params={"q": "Sea"})
    assert browse.status_code == 200, browse.text
    assert browse.json()["items"][0]["followed_list_id"] is None
    preview = await client.get("/api/discovery/lists/91")
    assert preview.status_code == 200, preview.text
    assert preview.json()["items"][0]["work"]["availability"]["ebook"]
    assert preview.json()["items"][1]["work"] is None
    result = await follow(client)
    assert result.status_code == 200, result.text
    data = result.json()
    async with database() as db:
        item = await db.get(BookList, UUID(data["list_id"]))
        sub = await db.get(ListSubscription, UUID(data["subscription_id"]))
        assert not item.shared and item.name == "Sea stories"
        assert sub.state == "queued" and sub.enabled
        assert await db.scalar(select(func.count()).select_from(ListEntry)) == 0
    for _ in range(4):
        await get_queue().run_worker_async(wait=False)
        await asyncio.sleep(1.1)
    async with database() as db:
        sub = await db.get(ListSubscription, UUID(data["subscription_id"]))
        assert sub.last_success_at and sub.state == "idle", sub.message
        assert await db.scalar(select(func.count()).select_from(ListEntry)) == 2
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
        assert await db.scalar(select(func.count()).select_from(ListAcquisitionPolicy)) == 0
        cached = list(await db.scalars(select(ProviderCache)))
        assert cached  # Search IDs and public book metadata may be reused.
        assert all("lists" not in row.value.get("data", {}) for row in cached)
    assert (await follow(client)).json() == data
    assert (await client.get("/api/discovery/lists")).json()["items"][0][
        "followed_list_id"
    ] == data["list_id"]
    assert len([c for c in provider["calls"] if c["query"] == PAGE]) == 4


async def test_concurrent_follows_reuse_one_subscription_and_preserve_paused_settings(
    client, admin, database, provider
):
    await connect(client)
    results = await asyncio.gather(
        follow(client, "first-command"), follow(client, "second-command")
    )
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({r.json()["list_id"] for r in results}) == 1
    sid = UUID(results[0].json()["subscription_id"])
    async with database() as db, db.begin():
        sub = await db.get(ListSubscription, sid)
        sub.enabled = False
        item = await db.get(BookList, sub.list_id)
        item.name = "My existing name"
    result = await follow(client, "third-command", name="Replacement name")
    assert result.json()["reused"]
    async with database() as db:
        sub = await db.get(ListSubscription, sid)
        assert not sub.enabled and (await db.get(BookList, sub.list_id)).name == "My existing name"
        assert await db.scalar(select(func.count()).select_from(BookList)) == 1
        assert (
            await db.scalar(
                select(func.count()).select_from(Operation).where(Operation.kind == "lists.sync")
            )
            == 1
        )


@pytest.mark.parametrize("change", ["account", "role", "private", "oversize", "unavailable"])
async def test_changes_during_provider_io_prevent_creating_an_unusable_follow(
    client, admin, database, provider, change
):
    await connect(client)

    async def hook():
        if change == "account":
            async with database() as db, db.begin():
                await db.execute(
                    update(CatalogAccount).values(generation=CatalogAccount.generation + 1)
                )
        elif change == "role":
            async with database() as db, db.begin():
                await db.execute(update(User).values(role="viewer"))
        elif change == "private":
            provider["public"] = False
        elif change == "oversize":
            provider["count"] = 5001
        else:
            provider["failure"] = 503

    provider["hook"] = hook
    result = await follow(client)
    assert (
        result.status_code
        == {"account": 409, "role": 403, "private": 404, "oversize": 422, "unavailable": 503}[
            change
        ]
    ), result.text
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(BookList)) == 0
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0


async def test_idempotency_conflict_and_detached_receipt_do_not_recreate_follow(
    client, admin, database, provider
):
    await connect(client)
    result = (await follow(client)).json()
    assert (await follow(client, name="different")).status_code == 409
    async with database() as db, db.begin():
        await db.execute(
            delete(ListSubscription).where(ListSubscription.id == UUID(result["subscription_id"]))
        )
    assert (await follow(client)).status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(BookList)) == 1
        assert await db.scalar(select(func.count()).select_from(ListSubscription)) == 0


async def test_enqueue_failure_rolls_back_follow_and_receipt(
    client, admin, database, provider, monkeypatch
):
    from app.domain import list_subscriptions

    await connect(client)

    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic enqueue failure")

    monkeypatch.setattr(list_subscriptions, "enqueue", fail)
    with pytest.raises(RuntimeError, match="enqueue failure"):
        await follow(client)
    async with database() as db:
        for model in [BookList, ListSubscription, Operation]:
            assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_follow_references_and_ownership_are_scoped_and_viewer_cannot_follow(
    client, admin, database, provider
):
    await connect(client)
    await follow(client)
    async with database() as db, db.begin():
        work = await add_work(db, "42")
        await add_owned(db, work)
    await login_member(client, "viewer")
    await connect(client)
    preview = (await client.get("/api/discovery/lists/91")).json()
    assert preview["info"]["followed_list_id"] is None
    assert not preview["items"][0]["work"]["availability"]["owned"]
    assert (await follow(client)).status_code == 403


async def test_public_reads_do_not_serve_cached_private_lists(client, admin, provider):
    await connect(client)
    assert (await client.get("/api/discovery/lists/91")).status_code == 200
    provider["public"] = False
    assert (await client.get("/api/discovery/lists/91")).status_code == 404
    assert (await client.get("/api/discovery/lists")).json()["items"] == []


async def test_existing_follow_lock_order_cannot_deadlock_with_subscription_command(
    client, admin, database, provider, monkeypatch
):
    from app.domain import community_lists
    from app.domain.operations import try_transaction_lock

    await connect(client)
    created = (await follow(client)).json()
    reached, proceed = asyncio.Event(), asyncio.Event()
    original = community_lists.followed

    async def gated(db, owner):
        value = await original(db, owner)
        reached.set()
        await proceed.wait()
        return value

    monkeypatch.setattr(community_lists, "followed", gated)
    task = None
    try:
        async with database() as db, db.begin():
            await db.scalar(
                select(BookList).where(BookList.id == UUID(created["list_id"])).with_for_update()
            )
            task = asyncio.create_task(follow(client, "colliding-sync-command"))
            await asyncio.wait_for(reached.wait(), 5)
            # A normal sync already holding the list must be able to take its command lock.
            assert await try_transaction_lock(db, f"operation:{admin['id']}:colliding-sync-command")
            db.add(
                Operation(
                    owner_id=UUID(admin["id"]),
                    kind="lists.sync",
                    idempotency_key="colliding-sync-command",
                    status="completed",
                    payload={},
                )
            )
            proceed.set()
        response = await asyncio.wait_for(task, 5)
        assert response.status_code == 409
    finally:
        proceed.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_recovery_mode_blocks_follow_before_network(client, admin, provider, monkeypatch):
    from app.config import get_settings

    await connect(client)
    monkeypatch.setattr(get_settings(), "recovery_mode", True)
    response = await follow(client)
    assert response.status_code == 409 and not provider["calls"]


@pytest.mark.parametrize("external_id", ["0", "not-a-list", "2147483648"])
async def test_invalid_preview_identifier_fails_before_provider_io(
    client, admin, provider, external_id
):
    await connect(client)
    assert (await client.get(f"/api/discovery/lists/{external_id}")).status_code == 422
    assert not provider["calls"]
