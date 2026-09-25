"""Library series suggestions stay on accepted Hardcover ids and never start downloads."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, text, update

from app.db.models import (
    AcquisitionIntent,
    CatalogAccount,
    CatalogSeries,
    LibraryAsset,
    Operation,
    SeriesGapBaseline,
    SeriesGapDismissal,
    SeriesGapSighting,
    SeriesMembership,
    User,
    WorkMetadataSource,
)
from app.domain.series_gap_watch import MAX_PER_RUN, record_sightings, scan_user
from app.security import encrypt_secrets
from tests.integration.test_catalog_series import record
from tests.integration.test_series_discovery import add_book, add_series, get_shelf

pytestmark = pytest.mark.integration


async def account(db, user_id, *, enabled=True, suggest=True):
    db.add(
        CatalogAccount(
            user_id=UUID(str(user_id)),
            encrypted_token=encrypt_secrets({"token": "gap-token"}),
            enabled=enabled,
            suggest_series_gaps=suggest,
            generation=1,
            status="connected",
        )
    )


async def link(db, work, series):
    db.add(
        WorkMetadataSource(
            work_id=work.id,
            provider="hardcover",
            external_id=f"book-{work.id.hex[:8]}",
            fetched_at=datetime.now(UTC),
            accepted=True,
            snapshot={
                "provider": "hardcover",
                "external_id": "1",
                "title": work.title,
                "authors": list(work.authors),
                "series": series,
            },
        )
    )


def series_entry(external_id, name="Coast", compilation=False):
    return {
        "external_id": external_id,
        "name": name,
        "position": "1",
        "compilation": compilation,
    }


async def refresh_ids(db):
    rows = list(
        await db.scalars(
            select(Operation).where(
                Operation.kind == "catalog.series.refresh",
                Operation.status == "queued",
            )
        )
    )
    return sorted(row.payload["command"]["external_id"] for row in rows)


async def test_owned_hardcover_series_is_queued_and_loose_names_are_not(client, admin, database):
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        ignored = await add_book(db, "Unowned")
        library = await add_owned_library(db, owned)
        await link(db, owned, [series_entry("41"), series_entry("42", compilation=True)])
        await link(db, ignored, [series_entry("77")])
        await db.execute(
            update(LibraryAsset)
            .where(LibraryAsset.library_id == library.id)
            .values(
                metadata_snapshot={"series": [{"name": "Audiobookshelf Coast", "sequence": "1"}]}
            )
        )
        await account(db, admin["id"])
    await scan_user(UUID(admin["id"]))
    async with database() as db:
        assert await refresh_ids(db) == ["41"]
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0


async def add_owned_library(db, work):
    from tests.integration.test_discovery import add_owned

    return await add_owned(db, work)


async def test_scan_skips_dismissed_fresh_and_recent_failures_and_respects_the_cap(
    client, admin, database
):
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        await add_owned_library(db, owned)
        await link(
            db,
            owned,
            [series_entry(str(number)) for number in (1, 2, 3, 4, 5)],
        )
        now = datetime.now(UTC)
        fresh = CatalogSeries(
            owner_id=UUID(admin["id"]),
            provider="hardcover",
            external_id="1",
            name="Fresh",
            fetched_at=now,
        )
        stale = CatalogSeries(
            owner_id=UUID(admin["id"]),
            provider="hardcover",
            external_id="2",
            name="Stale",
            fetched_at=now - timedelta(days=8),
        )
        failed_series = CatalogSeries(
            owner_id=UUID(admin["id"]),
            provider="hardcover",
            external_id="5",
            name="Failed",
            fetched_at=None,
        )
        failed = Operation(
            owner_id=UUID(admin["id"]),
            kind="catalog.series.refresh",
            idempotency_key="failed-series-gap",
            status="failed",
            message="Rate limited",
        )
        db.add_all([fresh, stale, failed_series, failed])
        await db.flush()
        failed_series.operation_id = failed.id
        db.add(
            SeriesGapDismissal(
                user_id=UUID(admin["id"]),
                provider="hardcover",
                external_id="4",
            )
        )
        await account(db, admin["id"])
    await scan_user(UUID(admin["id"]))
    async with database() as db:
        assert await refresh_ids(db) == ["2", "3"]
        assert len(await refresh_ids(db)) == MAX_PER_RUN
    await scan_user(UUID(admin["id"]))
    async with database() as db:
        assert await refresh_ids(db) == ["2", "3"]


async def test_disabled_account_and_suggestion_flag_do_not_scan(client, admin, database):
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        await add_owned_library(db, owned)
        await link(db, owned, [series_entry("41")])
        await account(db, admin["id"], suggest=False)
    await scan_user(UUID(admin["id"]))
    async with database() as db, db.begin():
        assert await refresh_ids(db) == []
        await db.execute(update(CatalogAccount).values(suggest_series_gaps=True, enabled=False))
    await scan_user(UUID(admin["id"]))
    async with database() as db:
        assert await refresh_ids(db) == []


async def test_first_observation_is_seen_and_a_later_book_is_new(client, admin, database):
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        gap = await add_book(db, "Gap")
        future = await add_book(db, "Future")
        await add_owned_library(db, owned)
        series = await add_series(
            db,
            admin["id"],
            [(owned, {}), (future, {"release_date": "2999-01-01"})],
            external_id="9",
        )
        await account(db, admin["id"])
        user = await db.get(User, UUID(admin["id"]))
        await record_sightings(db, user, series)
        series_id, gap_id = series.id, gap.id
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SeriesGapBaseline)) == 1
        assert await db.scalar(select(func.count()).select_from(SeriesGapSighting)) == 0
    async with database() as db, db.begin():
        db.add(
            SeriesMembership(
                series_id=series_id,
                work_id=gap_id,
                external_id="gap",
                snapshot=record(2, title="Gap"),
                present=True,
            )
        )
        user = await db.get(User, UUID(admin["id"]))
        await record_sightings(db, user, await db.get(CatalogSeries, series_id))
    shelf = await get_shelf(client)
    item = shelf["items"][0]
    assert item["unseen"] == shelf["unseen"] == 1
    assert item["books"][0]["unseen"] is True
    assert item["books"][0]["work"]["id"] == str(gap_id)
    seen = await client.post("/api/discovery/series/seen", json={"external_id": "9"})
    assert seen.status_code == 204, seen.text
    assert (await get_shelf(client))["unseen"] == 0
    dismissed = await client.post("/api/discovery/series/9/dismiss")
    assert dismissed.status_code == 204, dismissed.text
    assert (await get_shelf(client))["items"] == []
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SeriesGapSighting)) == 0
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.kind == "series.requests")
            )
            == 0
        )


async def test_full_list_returns_every_published_gap(client, admin, database):
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        gaps = [await add_book(db, f"Gap {index}") for index in range(4)]
        await add_owned_library(db, owned)
        await add_series(db, admin["id"], [(owned, {}), *[(gap, {}) for gap in gaps]])
    preview = await get_shelf(client)
    assert len(preview["items"][0]["books"]) == 3
    assert preview["items"][0]["missing"] == 4
    complete = await get_shelf(client, full="true")
    assert len(complete["items"][0]["books"]) == 4
    assert complete["suggestions_enabled"] is False


async def test_schedule_enqueues_each_due_account_once(client, admin, database):
    async with database() as db, db.begin():
        await account(db, admin["id"])
    from app.domain.series_gap_watch import schedule

    await schedule()
    await schedule()
    async with database() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM book_queue.procrastinate_jobs "
                    "WHERE task_name = 'series.library_scan'"
                )
            )
            == 1
        )


async def test_suggestion_switch_queues_a_scan_without_a_download(client, admin, database):
    missing = await client.put("/api/metadata/account/series-suggestions", json={"enabled": True})
    assert missing.status_code == 409, missing.text
    saved = await client.put("/api/metadata/account", json={"token": "gap-token", "enabled": True})
    assert saved.status_code == 200, saved.text
    assert saved.json()["suggest_series_gaps"] is False
    enabled = await client.put("/api/metadata/account/series-suggestions", json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["suggest_series_gaps"] is True
    async with database() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM book_queue.procrastinate_jobs "
                    "WHERE task_name = 'series.library_scan'"
                )
            )
            == 1
        )
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
    disabled = await client.put("/api/metadata/account/series-suggestions", json={"enabled": False})
    assert disabled.json()["suggest_series_gaps"] is False


async def test_loaded_catalog_is_baselined_before_the_next_refresh(client, admin, database):
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        gaps = [await add_book(db, f"Gap {index}") for index in range(4)]
        await add_owned_library(db, owned)
        await link(db, owned, [series_entry("41")])
        await add_series(
            db, admin["id"], [(owned, {}), *[(gap, {}) for gap in gaps]], external_id="41"
        )
        await account(db, admin["id"])
    await scan_user(UUID(admin["id"]))
    async with database() as db:
        assert await refresh_ids(db) == []
        assert await db.scalar(select(func.count()).select_from(SeriesGapBaseline)) == 1
        seen = list(await db.scalars(select(SeriesGapSighting)))
        assert len(seen) == 4 and all(row.seen_at for row in seen)
        series_id = await db.scalar(select(CatalogSeries.id))
    async with database() as db, db.begin():
        later = await add_book(db, "Later")
        db.add(
            SeriesMembership(
                series_id=series_id,
                work_id=later.id,
                external_id="later",
                snapshot=record(9, title="Later"),
                present=True,
            )
        )
        user = await db.get(User, UUID(admin["id"]))
        await record_sightings(db, user, await db.get(CatalogSeries, series_id))
        later_id = later.id
    preview = await get_shelf(client)
    assert preview["items"][0]["unseen"] == 1
    assert preview["items"][0]["books"][0]["work"]["title"] == "Gap 0"
    complete = await get_shelf(client, full="true")
    assert complete["items"][0]["books"][-1]["work"]["id"] == str(later_id)
    assert complete["items"][0]["books"][0]["work"]["title"] == "Gap 0"


async def test_stale_catalog_is_baselined_and_then_refreshed(client, admin, database):
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        gap = await add_book(db, "Gap")
        await add_owned_library(db, owned)
        await link(db, owned, [series_entry("41")])
        await add_series(
            db,
            admin["id"],
            [(owned, {}), (gap, {})],
            external_id="41",
            fetched_at=datetime.now(UTC) - timedelta(days=8),
        )
        await account(db, admin["id"])
    await scan_user(UUID(admin["id"]))
    async with database() as db:
        assert await refresh_ids(db) == ["41"]
        sightings = list(await db.scalars(select(SeriesGapSighting)))
        assert len(sightings) == 1 and sightings[0].seen_at is not None


async def test_failure_backoff_follows_the_last_change(client, admin, database):
    now = datetime.now(UTC)
    async with database() as db, db.begin():
        owned = await add_book(db, "Owned")
        await add_owned_library(db, owned)
        await link(db, owned, [series_entry("8")])
        failed = Operation(
            owner_id=UUID(admin["id"]),
            kind="catalog.series.refresh",
            idempotency_key="expired-series-gap",
            status="failed",
            message="Series observation expired",
            created_at=now - timedelta(minutes=40),
            updated_at=now,
        )
        row = CatalogSeries(
            owner_id=UUID(admin["id"]),
            provider="hardcover",
            external_id="8",
            name="Slow",
            fetched_at=now - timedelta(days=8),
        )
        db.add_all([failed, row])
        await db.flush()
        row.operation_id = failed.id
        await account(db, admin["id"])
    await scan_user(UUID(admin["id"]))
    async with database() as db:
        assert await refresh_ids(db) == []
    async with database() as db, db.begin():
        await db.execute(update(Operation).values(updated_at=now - timedelta(minutes=20)))
    await scan_user(UUID(admin["id"]))
    async with database() as db:
        assert await refresh_ids(db) == ["8"]
