import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.adapters.catalog_providers import Hardcover
from app.adapters.contracts import AdapterError, FailureKind
from app.db.models import Operation, ProviderBudget, ProviderCache
from app.domain.catalog_network import CatalogGateway
from app.domain.catalog_refresh import run
from tests.integration import test_discovery as discovery_fixtures

pytestmark = pytest.mark.integration
catalog_provider = discovery_fixtures.provider
connect = discovery_fixtures.connect


async def test_catalog_cache_coalesces_cold_and_expired_reads(database):
    calls = []

    async def response(request):
        calls.append(request)
        await asyncio.sleep(0.03)
        return httpx.Response(200, json={"title": "Saved title"})

    async def read():
        async with CatalogGateway(
            "openlibrary", "public", transport=httpx.MockTransport(response)
        ) as gateway:
            return await gateway.request("GET", "works/OL1W.json")

    assert await asyncio.gather(*(read() for _ in range(6))) == [{"title": "Saved title"}] * 6
    assert len(calls) == 1
    async with database() as db, db.begin():
        cached = await db.scalar(select(ProviderCache))
        assert cached.expires_at - cached.fetched_at == timedelta(days=1)
        cached.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await asyncio.gather(*(read() for _ in range(6))) == [{"title": "Saved title"}] * 6
    assert len(calls) == 2


async def test_shelf_reordering_reuses_details_and_preserves_new_rank(database):
    from app.adapters.hardcover_discovery import load_ids

    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "books": [
                        {"id": key, "title": f"Book {key}", "cached_contributors": []}
                        for key in (42, 43)
                    ]
                }
            },
        )

    async with CatalogGateway(
        "hardcover", "reader:1", "token", transport=httpx.MockTransport(response)
    ) as gateway:
        query = Hardcover(gateway.request).query
        first = await load_ids(query, [42, 43])
        second = await load_ids(query, [43, 42])
    assert [book.external_id for book in first.items] == ["42", "43"]
    assert [book.external_id for book in second.items] == ["43", "42"]
    assert len(calls) == 1


async def test_cleanup_does_not_deadlock_concurrent_expired_fills(database, monkeypatch):
    from app.domain import cache_entries

    old = datetime.now(UTC) - timedelta(days=9)
    async with database() as db, db.begin():
        db.add_all(
            ProviderCache(key=key, value={"old": True}, fetched_at=old, expires_at=old)
            for key in ("first", "second")
        )
    barrier = asyncio.Barrier(2)
    prune = cache_entries.prune

    async def simultaneous_cleanup(db, now):
        # Both transactions hold a freshly updated row before either prunes.
        await db.flush()
        await barrier.wait()
        await prune(db, now)

    async def load():
        return {"new": True}

    monkeypatch.setattr(cache_entries, "prune", simultaneous_cleanup)
    values = await asyncio.wait_for(
        asyncio.gather(
            *(
                cache_entries.read_through(key, load, fresh_for=timedelta(hours=1))
                for key in ("first", "second")
            )
        ),
        timeout=5,
    )
    assert values == [({"new": True}, False)] * 2
    async with database() as db:
        for key in ("first", "second"):
            assert (await db.get(ProviderCache, key)).value == {"new": True}


async def test_reviews_refresh_without_refetching_author_biographies(database):
    calls = []
    rating = 3

    def response(request):
        import json

        query = json.loads(request.content)["query"]
        activity = "ReaderBookReviews" in query
        calls.append(activity)
        return httpx.Response(
            200,
            json={
                "data": {
                    "books": [{"id": 42, "rating": rating, "ratings_count": 10}]
                    if activity
                    else [
                        {
                            "id": 42,
                            "slug": "a-book",
                            "contributions": [
                                {
                                    "contribution": "Author",
                                    "author": {"id": 7, "name": "Writer", "bio": "Saved biography"},
                                }
                            ],
                        }
                    ],
                    **({"user_books": []} if activity else {}),
                }
            },
        )

    async with CatalogGateway(
        "hardcover", "reader:1", "token", transport=httpx.MockTransport(response)
    ) as gateway:
        reader = Hardcover(gateway.request)
        first = await reader.reader_details("42")
        assert first.rating == 3 and first.authors[0].bio == "Saved biography"
        async with database() as db, db.begin():
            activity = await db.get(ProviderCache, gateway.used_keys[-1])
            descriptive = await db.get(ProviderCache, gateway.used_keys[0])
            assert descriptive.expires_at - descriptive.fetched_at == timedelta(days=1)
            assert activity.expires_at - activity.fetched_at == timedelta(hours=1)
            activity.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        rating = 4
        second = await reader.reader_details("42")
        assert second.rating == 4 and second.authors == first.authors
        assert calls == [False, True, True]


async def test_discover_serves_saved_data_and_queues_one_refresh(
    client, admin, database, catalog_provider
):
    await connect(client)
    path = "/api/discovery/hardcover/trending"
    first = (await client.get(path)).json()
    async with database() as db, db.begin():
        await db.execute(
            update(ProviderCache).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    count = len(catalog_provider["calls"])
    for _ in range(3):
        pending = (await client.get(path)).json()
        assert pending["items"] == first["items"] and pending["stale"]
    assert len(catalog_provider["calls"]) == count
    async with database() as db:
        jobs = list(await db.scalars(select(Operation).where(Operation.kind == "catalog.refresh")))
        assert len(jobs) == 1
        assert "token" not in str(jobs[0].payload)
    from app.jobs.queue import get_queue

    await asyncio.wait_for(
        get_queue().run_worker_async(queues=["catalog-cache"], wait=False, concurrency=1),
        timeout=15,
    )
    refreshed = (await client.get(path)).json()
    assert not refreshed["stale"] and refreshed["items"] == first["items"]
    assert len(catalog_provider["calls"]) > count


async def test_catalog_refresh_stops_after_credential_rotation(
    client, admin, database, catalog_provider
):
    await connect(client)
    path = "/api/discovery/hardcover/trending"
    await client.get(path)
    async with database() as db, db.begin():
        await db.execute(
            update(ProviderCache).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await client.get(path)
    async with database() as db:
        job = await db.scalar(select(Operation).where(Operation.kind == "catalog.refresh"))
    await client.put("/api/metadata/account", json={"token": "rotated-token"})
    count = len(catalog_provider["calls"])
    await run(job.id)
    assert len(catalog_provider["calls"]) == count
    async with database() as db:
        assert (await db.get(Operation, job.id)).status == "cancelled"


async def test_cover_cleanup_preserves_catalog_outage_fallback(
    client, admin, database, monkeypatch
):
    now = datetime.now(UTC)
    async with database() as db, db.begin():
        db.add_all(
            [
                ProviderCache(
                    key="recent",
                    value={"title": "Keep me"},
                    fetched_at=now - timedelta(hours=2),
                    expires_at=now - timedelta(hours=1),
                ),
                ProviderCache(
                    key="old",
                    value={},
                    fetched_at=now - timedelta(days=10),
                    expires_at=now - timedelta(days=8),
                ),
            ]
        )

    async def image(_):
        return b"image"

    monkeypatch.setattr("app.domain.cover_cache.fetch_cover", image)
    assert (
        await client.get(
            "/api/catalog/cover-image", params={"url": "https://assets.hardcover.app/cover.jpg"}
        )
    ).status_code == 200
    async with database() as db:
        assert await db.get(ProviderCache, "recent")
        assert not await db.get(ProviderCache, "old")


async def test_expired_cache_owner_cannot_delete_a_newer_fill(database):
    from app.domain.cache_entries import read_through

    started, finish = asyncio.Event(), asyncio.Event()

    async def failed():
        started.set()
        await finish.wait()
        raise AdapterError(FailureKind.PERMISSION, "Old request failed")

    async def replacement():
        return {"title": "New evidence"}

    pending = asyncio.create_task(read_through("fenced", failed, fresh_for=timedelta(hours=1)))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with database() as db, db.begin():
            lease = await db.get(ProviderBudget, "cache-fill:fenced")
            lease.next_request_at = datetime.now(UTC) - timedelta(seconds=1)
        await read_through("fenced", replacement, fresh_for=timedelta(hours=1))
        finish.set()
        with pytest.raises(AdapterError):
            await pending
        async with database() as db:
            assert (await db.get(ProviderCache, "fenced")).value == {"title": "New evidence"}
    finally:
        finish.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_delayed_parser_failure_preserves_replacement_cache(database):
    payload = {"title": ["Malformed title"]}

    def response(_):
        return httpx.Response(200, json=payload)

    class Gateway(CatalogGateway):
        async def reserve(self):
            pass

    async with Gateway(
        "openlibrary", "public", transport=httpx.MockTransport(response)
    ) as rejected:
        await rejected.request("GET", "works/OL1W.json")
        key = rejected.used_keys[0]
        payload = {"title": "Corrected title"}
        async with Gateway(
            "openlibrary", "public", force=True, transport=httpx.MockTransport(response)
        ) as replacement:
            returned = await replacement.request("GET", "works/OL1W.json")
            # Parsing an older response finishes after another request refreshed it.
            await rejected.invalidate()
            async with database() as db:
                cached = await db.get(ProviderCache, key)
                assert cached is not None and cached.value == payload
            # A rejection of the current response must still evict that response.
            # Adapter normalization must not change which stored response is rejected.
            returned["title"] = "Normalized by adapter"
            await replacement.invalidate()
            async with database() as db:
                assert await db.get(ProviderCache, key) is None


async def test_rejected_graphql_refresh_discards_old_cached_response(database):
    state = {"denied": False}

    def response(_):
        return httpx.Response(
            200,
            json={"errors": [{"message": "Forbidden"}]}
            if state["denied"]
            else {"data": {"books": [{"id": 42}]}},
        )

    async with CatalogGateway(
        "hardcover", "user:1", "token", transport=httpx.MockTransport(response)
    ) as gateway:
        await gateway.request(
            "POST", "v1/graphql", json={"query": "query Example { books { id } }"}
        )
        async with database() as db, db.begin():
            cached = await db.get(ProviderCache, gateway.used_keys[0])
            cached.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        state["denied"] = True
        assert "errors" in await gateway.request(
            "POST", "v1/graphql", json={"query": "query Example { books { id } }"}
        )
        async with database() as db:
            assert not await db.get(ProviderCache, gateway.used_keys[0])


async def test_post_restore_browsing_uses_a_new_refresh_operation(
    client, admin, database, catalog_provider
):
    from uuid import UUID

    from app.db.models import RestoreCheckpoint
    from app.domain.catalog_refresh import schedule
    from app.recovery_queue import seal
    from tests.integration.test_recovery_scan_workflow import pause

    await connect(client)
    await client.get("/api/discovery/hardcover/trending")
    async with database() as db, db.begin():
        await db.execute(
            update(ProviderCache).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await client.get("/api/discovery/hardcover/trending")
    async with database() as db:
        old = await db.scalar(select(Operation).where(Operation.kind == "catalog.refresh"))
    checkpoint = await pause(database, admin)
    async with database() as db, db.begin():
        await seal(db, checkpoint)
        (await db.get(RestoreCheckpoint, checkpoint)).active = False
    payload = old.payload
    await schedule(
        UUID(admin["id"]),
        payload["provider"],
        payload["generation"],
        payload["operation"],
        payload["args"],
    )
    async with database() as db:
        jobs = list(await db.scalars(select(Operation).where(Operation.kind == "catalog.refresh")))
        assert len(jobs) == 2 and jobs[0].id != jobs[1].id
