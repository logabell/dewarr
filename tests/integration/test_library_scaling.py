"""Boundary regressions for large-library workflows, using small byte budgets."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text, update

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.contracts import AdapterError, FailureKind, ResponseTooLarge
from app.db.models import InventoryItemState, LibraryAsset, Operation, Work
from app.domain.catalog_titles import display_title_sql
from app.domain.library_matching import match_library
from app.importing.ownership import already_owned
from app.jobs.queue import get_queue
from app.jobs.worker import run_pools
from tests.contracts.test_audiobookshelf import ABSFixture, book, connect, sync, titled
from tests.integration.test_library_matching import hardcover, match_operations, opt_in

pytestmark = pytest.mark.integration


async def test_detail_byte_budget_splits_batches_and_isolates_one_large_item():
    items = {str(n): book(str(n)) for n in range(5)}
    items["2"]["media"]["metadata"]["description"] = "x" * 6000
    calls = []

    async def handle(request):
        ids = json.loads(request.content)["libraryItemIds"]
        calls.append(ids)
        return httpx.Response(200, json={"libraryItems": [items[i] for i in ids]})

    async with Audiobookshelf(
        "http://abs.test", "token", transport=httpx.MockTransport(handle)
    ) as api:
        api.detail_bytes = 3000
        observed = [item async for item in api.inventory_items("library-one", list(items.values()))]
        assert [item.id for item in observed] == list(items)
        assert [item.id for item in observed if item.unreadable] == ["2"]
        assert all(item.full_audio for item in observed if item.id != "2")
        assert any(len(batch) == 1 for batch in calls)
        # A confirmation must never accept the inventory's incomplete placeholder.
        with pytest.raises(ResponseTooLarge):
            await api.expanded(["2"])


async def test_summary_rebatching_preserves_offsets_above_old_catalog_cap(monkeypatch):
    calls = []

    async def request(self, method, path, *, params, **kwargs):
        size, page = params["limit"], params["page"]
        calls.append((page, size))
        if size > 5:
            raise ResponseTooLarge(1000, 1001)
        return {
            "total": 100001,
            "results": [{"id": str(n)} for n in range(page * size, (page + 1) * size)],
        }

    monkeypatch.setattr(Audiobookshelf, "request", request)
    async with Audiobookshelf("http://abs.test", "token") as api:
        first, total = await api.page("library-one", 0)
        second, _ = await api.page("library-one", 1)
        assert total == 100001
        assert [row["id"] for row in first + second] == [str(n) for n in range(200)]
        assert [size for _, size in calls[:4]] == [100, 50, 25, 5]
        assert all(size == 5 for _, size in calls[4:])


class RichSummary(ABSFixture):
    async def handle(self, request):
        response = await super().handle(request)
        if request.url.path.endswith("/items"):
            payload = response.json()
            for row in payload["results"]:
                item = self.items[row["id"]]
                row.update(path=f"/private/library/{row['id']}", media=copy.deepcopy(item["media"]))
            return httpx.Response(200, json=payload)
        return response


async def test_unchanged_inventory_reuses_details_but_changed_or_expired_evidence_does_not(
    client, admin, database
):
    connection = await connect(client)
    fixture = RichSummary({"one": book("one"), "two": book("two")})
    await sync(client, connection, fixture, "initial-sync")
    fixture.calls.clear()
    unchanged = await sync(client, connection, fixture, "unchanged")
    assert not any(path.endswith("batch/get") for path in fixture.calls)
    async with database() as db:
        assert (await db.get(Operation, unchanged)).payload["inventory"] == {
            "items": 2,
            "details_read": 0,
            "details_reused": 2,
        }
        assets = list(await db.scalars(select(LibraryAsset)))
        assert len(assets) == 2 and all(asset.state == "present" for asset in assets)
    # Pre-format-cache rows must refresh, even with unchanged upstream summaries.
    async with database() as db, db.begin():
        await db.execute(update(InventoryItemState).values(schema_version=1, observed_media=[]))
    fixture.calls.clear()
    legacy = await sync(client, connection, fixture, "legacy-cache-sync")
    assert any(path.endswith("batch/get") for path in fixture.calls)
    async with database() as db:
        assert (await db.get(Operation, legacy)).payload["inventory"]["details_reused"] == 0
    # A summary change invalidates the cache even when upstream forgot updatedAt.
    fixture.items["one"]["media"]["metadata"]["narrators"] = ["New Narrator"]
    fixture.calls.clear()
    await sync(client, connection, fixture, "changed-sync")
    assert sum(path.endswith("batch/get") for path in fixture.calls) == 1
    async with database() as db, db.begin():
        await db.execute(
            update(InventoryItemState).values(checked_at=datetime.now(UTC) - timedelta(days=2))
        )
    fixture.calls.clear()
    await sync(client, connection, fixture, "expired-sync")
    assert any(path.endswith("batch/get") for path in fixture.calls)


@pytest.mark.parametrize("removed_medium", ["ebook", "audio"])
async def test_cached_inventory_keeps_removed_formats_missing(
    client, admin, database, removed_medium
):
    connection = await connect(client)
    other = book("other", ebook="epub")
    other["media"]["metadata"]["title"] = "Another Harbor"
    fixture = RichSummary({"one": book("one", ebook="epub"), "other": other})
    await sync(client, connection, fixture, "formats-initial-sync")
    fixture.items["one"] = book(
        "one", audio=removed_medium != "audio", ebook="epub" if removed_medium != "ebook" else None
    )
    await sync(client, connection, fixture, "formats-remove-sync")
    async with database() as db:
        removed = await db.scalar(
            select(LibraryAsset).where(
                LibraryAsset.external_id == "one", LibraryAsset.medium == removed_medium
            )
        )
        removed_id, missing_since = removed.id, removed.missing_since
        assert removed.state == "missing-suspected"
        assert not await already_owned(db, removed.version_id, removed.library_id)

    fixture.calls.clear()
    reused = await sync(client, connection, fixture, "formats-cached-sync")
    assert not any(path.endswith("batch/get") for path in fixture.calls)
    async with database() as db, db.begin():
        operation = await db.get(Operation, reused)
        assert operation.status == "completed"
        assert operation.payload["inventory"]["details_reused"] == 2
        removed = await db.get(LibraryAsset, removed_id)
        assert removed.state == "missing-suspected" and removed.missing_since == missing_since
        assert not await already_owned(db, removed.version_id, removed.library_id)
        survivors = list(
            await db.scalars(select(LibraryAsset).where(LibraryAsset.id != removed_id))
        )
        assert len(survivors) == 3 and all(asset.state == "present" for asset in survivors)
        removed.missing_since = datetime.now(UTC) - timedelta(minutes=6)

    await sync(client, connection, fixture, "formats-confirm-missing")
    assert "/abs/api/items/one" in fixture.calls
    async with database() as db:
        removed = await db.get(LibraryAsset, removed_id)
        assert removed.state == "missing-confirmed"
        assert not await already_owned(db, removed.version_id, removed.library_id)

    fixture.items["one"] = book("one", ebook="epub")
    await sync(client, connection, fixture, "formats-restore-sync")
    async with database() as db:
        restored = await db.get(LibraryAsset, removed_id)
        assert restored.state == "present" and restored.missing_since is None
        assert await already_owned(db, restored.version_id, restored.library_id)


async def test_cached_inventory_keeps_retired_formats_retired(client, admin, database):
    connection = await connect(client)
    fixture = RichSummary({"one": book("one", ebook="epub")})
    await sync(client, connection, fixture, "retired-initial-sync")
    async with database() as db, db.begin():
        await db.execute(update(LibraryAsset).values(state="stale"))
        await db.execute(
            update(LibraryAsset)
            .where(LibraryAsset.medium == "ebook")
            .values(state="intentionally-removed")
        )
    fixture.calls.clear()
    await sync(client, connection, fixture, "retired-cached-sync")
    assert not any(path.endswith("batch/get") for path in fixture.calls)
    async with database() as db:
        assets = {asset.medium: asset for asset in await db.scalars(select(LibraryAsset))}
        assert assets["ebook"].state == "intentionally-removed"
        assert assets["audio"].state == "present"


async def test_inventory_renews_lease_between_slow_backend_requests(
    client, admin, database, monkeypatch
):
    connection = await connect(client)
    elapsed, base = 0, datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return base + timedelta(seconds=elapsed)

    class SlowLibrary(ABSFixture):
        async def handle(self, request):
            nonlocal elapsed
            response = await super().handle(request)
            elapsed += 90
            return response

    monkeypatch.setattr("app.domain.inventory.datetime", Clock)
    monkeypatch.setattr("app.domain.inventory.time", SimpleNamespace(monotonic=lambda: elapsed))
    operation_id = await sync(
        client, connection, SlowLibrary({"one": book("one")}), "slow-library-sync"
    )
    async with database() as db:
        operation = await db.get(Operation, operation_id)
        assert operation.status == "completed", operation.message
        assert elapsed > 180


async def test_matching_continues_past_unmatched_first_batch(client, admin, database, monkeypatch):
    await opt_in(database, admin)
    connection = await connect(client)
    fixture = ABSFixture(
        {str(n): titled(str(n), f"Unknown Book {n}", ["Pierce Brown"]) for n in range(5)}
    )
    await sync(client, connection, fixture, "matching-pages")
    (operation,) = await match_operations(database)
    monkeypatch.setattr("app.domain.library_matching.BATCH", 2)
    monkeypatch.setattr("app.api.metadata.provider_call", hardcover([]))
    for checked in (2, 4, 5):
        await match_library(operation.id)
        async with database() as db:
            current = await db.get(Operation, operation.id)
            assert current.payload["checked"] == checked
            assert current.status == ("completed" if checked == 5 else "queued")
            if checked < 5:
                job = (
                    await db.execute(
                        text(
                            "SELECT args, lock, scheduled_at > now() AS delayed "
                            "FROM book_queue.procrastinate_jobs WHERE id=:id"
                        ),
                        {"id": current.job_id},
                    )
                ).one()
                assert job.args == {"operation_id": str(operation.id)}
                assert job.lock == f"library-match:{operation.id}" and job.delayed


async def test_matching_retries_rate_limited_book_without_advancing_cursor(
    client, admin, database, monkeypatch
):
    await opt_in(database, admin)
    connection = await connect(client)
    await sync(client, connection, ABSFixture({"one": book("one")}), "matching-cooldown")
    (operation,) = await match_operations(database)

    async def cooling_down(*args, **kwargs):
        raise AdapterError(FailureKind.RATE_LIMIT, "Wait", retry_after=90)

    monkeypatch.setattr("app.api.metadata.provider_call", cooling_down)
    await match_library(operation.id)
    async with database() as db:
        current = await db.get(Operation, operation.id)
        assert current.status == "queued" and current.payload["checked"] == 0
        assert current.payload["cursor"] is None
        delay = await db.scalar(
            text(
                "SELECT extract(epoch FROM scheduled_at - now()) "
                "FROM book_queue.procrastinate_jobs WHERE id=:id"
            ),
            {"id": current.job_id},
        )
        assert 80 < delay <= 90
    monkeypatch.setattr("app.api.metadata.provider_call", hardcover([]))
    await match_library(operation.id)
    async with database() as db:
        current = await db.get(Operation, operation.id)
        assert current.status == "completed" and current.payload["checked"] == 1


async def test_control_jobs_run_while_each_expensive_work_class_is_busy(database):
    queue = get_queue()
    release = asyncio.Event()
    started = {
        name: asyncio.Event() for name in ("imports", "inventory", "confirmation", "metadata")
    }
    finished = asyncio.Event()

    async def busy(name):
        started[name].set()
        await release.wait()

    async def control():
        finished.set()

    for name in started:
        task = queue.task(name=f"scaling.busy.{name}", queue=name)(busy)
        await task.defer_async(name=name)
    probe = queue.task(name="scaling.control", queue="system")(control)
    worker = asyncio.create_task(run_pools(queue))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 10)
        await probe.defer_async()
        await asyncio.wait_for(finished.wait(), 5)
        assert not release.is_set()
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_normalized_identity_lookup_can_use_its_index(database):
    async with database() as db, db.begin():
        work = Work(title="The Harbor (Unabridged)", authors=["Alex Morgan"])
        db.add(work)
        await db.flush()
        query = select(Work.id).where(display_title_sql(Work.title) == "the harbor")
        assert await db.scalar(query) == work.id
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        connection = await db.connection()
        compiled = query.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True})
        plan = await connection.exec_driver_sql("EXPLAIN " + str(compiled))
        assert "ix_works_display_title" in "\n".join(plan.scalars())
