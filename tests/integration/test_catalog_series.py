# ruff: noqa: F811
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.adapters.catalog_types import BookData
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.hardcover_series import SeriesPage
from app.db.models import (
    AcquisitionIntent,
    CatalogAccount,
    CatalogSeries,
    Operation,
    SeriesGapBaseline,
    SeriesGapSighting,
    SeriesMembership,
    User,
)
from app.domain import catalog_series as series
from app.jobs.retry import CatalogRetry
from app.security import encrypt_secrets
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_correction_migration import migrate

pytestmark = pytest.mark.integration


def record(key=1, book=42, **kwargs):
    return {
        "entry_id": str(key),
        "book": BookData(
            provider="hardcover", external_id=str(book), title=f"Book {book}", authors=["Writer"]
        ).model_dump(mode="json"),
        "position": str(key),
        "details": str(key),
        "compilation": False,
        "partial": False,
        "canonical_id": None,
        "release_date": "2020-01-01",
        **kwargs,
    }


@pytest.fixture
async def service(monkeypatch, database, admin):
    async with database() as db, db.begin():
        db.add(
            CatalogAccount(
                user_id=UUID(admin["id"]),
                encrypted_token=encrypt_secrets({"token": "series-private-token"}),
                generation=1,
                enabled=True,
            )
        )

    class Service:
        items = [record(), record(2, 43)]
        calls = []
        error = None
        callback = None

        async def page(self, owner, generation, token, external_id, cursor):
            self.calls.append((owner, generation, token, external_id, cursor))
            if self.callback:
                await self.callback()
            if self.error:
                raise self.error
            items = [r for r in self.items if int(r["entry_id"]) > cursor][:1]
            return SeriesPage(
                {"external_id": external_id, "name": "Series", "count": len(self.items)},
                deepcopy(items),
                int(items[-1]["entry_id"]) if items else cursor,
            )

    service = Service()
    monkeypatch.setattr(series, "fetch_page", service.page)
    return service


async def start(client, key="series-observation", external_id="9"):
    response = await client.post(
        f"/api/catalog/series/hardcover/{external_id}/refresh",
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["id"])


async def finish(database, operation):
    for _ in range(30):
        await series.run(operation)
        async with database() as db:
            status = (await db.get(Operation, operation)).status
        if status in {"completed", "failed", "cancelled"}:
            return status
    raise AssertionError("Series refresh did not finish")


async def detail(client, external_id="9", **query):
    response = await client.get(f"/api/catalog/series/hardcover/{external_id}", params=query)
    assert response.status_code == 200, response.text
    return response.json()


async def test_staged_refresh_reuses_known_work_without_downloads(
    client, database, service, catalog
):
    operation = await start(client)
    await series.run(operation)
    assert (await detail(client))["total"] == 0
    assert await finish(database, operation) == "completed"
    result = await detail(client)
    assert result["total"] == result["books"] == 2
    assert result["owned"] == result["ebook"] == 1 and result["audio"] == 0
    assert result["items"][0]["work"]["id"] == str(catalog["work"])
    assert "series-private-token" not in str(result)
    assert [call[-1] for call in service.calls] == [0, 1, 0, 1]
    async with database() as db:
        assert "stage" not in (await db.get(Operation, operation)).payload
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
    await series.run(operation)
    assert len(service.calls) == 4
    assert await start(client) == operation


async def test_completed_refresh_baselines_gaps_and_marks_a_later_book_new(
    client, database, service, catalog
):
    async with database() as db, db.begin():
        account = await db.scalar(select(CatalogAccount))
        account.suggest_series_gaps = True
    assert await finish(database, await start(client)) == "completed"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SeriesGapBaseline)) == 1
        first = list(await db.scalars(select(SeriesGapSighting)))
        assert first and all(row.seen_at for row in first)
    service.items = [*service.items, record(3, 99)]
    assert await finish(database, await start(client, "added-book")) == "completed"
    async with database() as db:
        rows = list(await db.scalars(select(SeriesGapSighting)))
        assert sum(row.seen_at is None for row in rows) == 1
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0


async def test_refresh_without_suggestions_does_not_baseline(client, database, service, catalog):
    assert await finish(database, await start(client)) == "completed"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SeriesGapBaseline)) == 0
        assert await db.scalar(select(func.count()).select_from(SeriesGapSighting)) == 0


async def test_changed_verification_preserves_prior_memberships(client, database, service):
    assert await finish(database, await start(client)) == "completed"
    original = await detail(client)
    operation = await start(client, "second-observation")
    for _ in range(2):
        await series.run(operation)
    service.items = [record(1, 44), record(2, 45)]
    assert await finish(database, operation) == "failed"
    changed = await detail(client)
    assert changed["generation"] == original["generation"] == 1
    assert changed["items"] == original["items"]
    assert changed["fetched_at"] == original["fetched_at"]


async def test_verified_removal_keeps_history_and_stable_reappearance(client, database, service):
    await finish(database, await start(client))
    old = await detail(client)
    service.items = []
    await finish(database, await start(client, "empty-observation"))
    assert (await detail(client))["total"] == 0
    async with database() as db:
        rows = list(await db.scalars(select(SeriesMembership)))
        assert len(rows) == 2 and not any(row.present for row in rows)
    service.items = [record()]
    await finish(database, await start(client, "reappeared-observation"))
    new = await detail(client)
    assert new["items"][0]["membership_id"] == old["items"][0]["membership_id"]
    assert new["items"][0]["work"]["id"] == old["items"][0]["work"]["id"]


@pytest.mark.parametrize("change", ["token", "disabled", "role", "inactive"])
async def test_access_or_operation_changes_fence_response(client, database, service, admin, change):
    operation = await start(client)

    async def callback():
        service.callback = None
        async with database() as db, db.begin():
            account = await db.get(CatalogAccount, UUID(admin["id"]))
            user = await db.get(User, UUID(admin["id"]))
            if change == "token":
                account.generation += 1
            elif change == "disabled":
                account.enabled = False
            elif change == "role":
                user.role = "viewer"
            else:
                user.active = False

    service.callback = callback
    assert await finish(database, operation) == "cancelled"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SeriesMembership)) == 0
        assert (await db.scalar(select(CatalogSeries))).generation == 0


async def test_ambiguous_special_and_duplicate_records_do_not_inflate_book_counts(
    client, database, service, catalog
):
    service.items = [
        record(),
        record(2, 42, position="1"),
        record(3, 43, position="1"),
        record(4, 44, compilation=True),
        record(5, 45, partial=True),
        record(6, 46, canonical_id="42"),
        record(7, 47, release_date="2099-01-01"),
        record(8, 48, release_date=None),
    ]
    await finish(database, await start(client))
    result = await detail(client)
    assert result["total"] == 8 and result["books"] == 4
    assert result["owned"] == 1
    assert all(r["ambiguous_position"] for r in result["items"][:3])
    assert result["items"][-2]["publication"] == "unreleased"
    assert result["items"][-1]["publication"] == "unknown"
    paged = await detail(client, offset=3, limit=2)
    assert len(paged["items"]) == 2 and paged["books"] == 4 and paged["total"] == 8


async def test_retry_and_exhaustion_are_bounded(client, database, service):
    service.error = AdapterError(FailureKind.UNAVAILABLE, "Provider unavailable")
    operation = await start(client)
    for _ in range(4):
        with pytest.raises(CatalogRetry):
            await series.run(operation)
    await series.run(operation)
    result = await detail(client)
    assert result["status"] == "failed" and result["generation"] == 0
    assert len(service.calls) == 5


async def test_atomic_publication_failure_keeps_observation_unpublished(
    client, database, service, monkeypatch
):
    operation = await start(client)
    for _ in range(3):
        await series.run(operation)
    # Final publication uses the same transaction as catalog resolution.
    original = series.catalog_match
    calls = 0

    async def broken(*args):
        nonlocal calls
        calls += 1
        result = await original(*args)
        if calls == 2:
            raise RuntimeError("injected publication failure")
        return result

    monkeypatch.setattr(series, "catalog_match", broken)
    with pytest.raises(RuntimeError, match="injected"):
        await series.run(operation)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SeriesMembership)) == 0
        assert (await db.scalar(select(CatalogSeries))).generation == 0
    monkeypatch.setattr(series, "catalog_match", original)
    async with database() as db, db.begin():
        op = await db.get(Operation, operation)
        op.payload = {**op.payload, "lease_until": None}
    assert await finish(database, operation) == "completed"
    assert (await detail(client))["books"] == 2


async def test_populated_migration_refuses_history_loss(client, database, service):
    await finish(database, await start(client))
    result = await migrate("downgrade", "0032_request_release_policy")
    assert result.returncode != 0 and "pre-upgrade backup" in result.stderr
    assert (await detail(client))["generation"] == 1


async def test_duplicate_commands_and_inflight_delivery_are_fenced(client, database, service):
    import asyncio

    operations = await asyncio.gather(*(start(client) for _ in range(5)))
    assert len(set(operations)) == 1
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    service.callback = hold
    first = asyncio.create_task(series.run(operations[0]))
    await asyncio.wait_for(entered.wait(), timeout=3)
    try:
        with pytest.raises(CatalogRetry):
            await series.run(operations[0])
    finally:
        release.set()
        await first
    service.callback = None
    assert len(service.calls) == 1
    assert await finish(database, operations[0]) == "completed"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(CatalogSeries)) == 1
    response = await client.post(
        "/api/catalog/series/hardcover/10/refresh",
        headers={"Idempotency-Key": "series-observation"},
    )
    assert response.status_code == 409


async def test_failed_enqueue_rolls_back_series_and_command(client, database, service, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("injected queue failure")

    monkeypatch.setattr(series, "enqueue", fail)
    with pytest.raises(RuntimeError, match="queue failure"):
        await start(client)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(CatalogSeries)) == 0
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0


async def test_series_and_inventory_scopes_are_independent(client, database, service, catalog):
    import httpx

    from app.db.models import LibraryGrant
    from app.main import create_app

    await finish(database, await start(client))
    user = (
        await client.post(
            "/api/auth/users",
            json={
                "username": "reader",
                "display_name": "Reader",
                "role": "member",
                "password": "a long reader password",
            },
        )
    ).json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as other:
        signed = await other.post(
            "/api/auth/login",
            json={
                "username": "reader",
                "password": "a long reader password",
            },
        )
        other.headers["X-CSRF-Token"] = signed.json()["csrf_token"]
        assert (await detail(other))["status"] == "not-loaded"
        denied = await other.post(
            "/api/catalog/series/hardcover/9/refresh",
            headers={"Idempotency-Key": "unconnected-refresh"},
        )
        assert denied.status_code == 409
        async with database() as db, db.begin():
            db.add(
                CatalogAccount(
                    user_id=UUID(user["id"]),
                    generation=1,
                    enabled=True,
                    encrypted_token=encrypt_secrets({"token": "other-token"}),
                )
            )
        await finish(database, await start(other))
        own = await detail(other)
        assert own["owned"] == 0
        assert own["items"][0]["work"]["id"] == str(catalog["work"])
        assert own["items"][1]["work"]["id"] != (await detail(client))["items"][1]["work"]["id"]
        async with database() as db, db.begin():
            db.add(LibraryGrant(user_id=UUID(user["id"]), library_id=catalog["library"]))
        assert (await detail(other))["owned"] == 1
        async with database() as db, db.begin():
            owner = await db.get(User, UUID(user["id"]))
            owner.role = "viewer"
        denied = await other.post(
            "/api/catalog/series/hardcover/9/refresh", headers={"Idempotency-Key": "viewer-refresh"}
        )
        assert denied.status_code == 403
        assert (await detail(other))["owned"] == 1


async def test_series_projection_follows_merge_and_undo_without_erasing_memberships(
    client, database, service, catalog
):
    from tests.integration.test_work_merges import merge

    await finish(database, await start(client))
    before = await detail(client)
    target = str(catalog["work"])
    change = await merge(client, before["items"][1]["work"]["id"], target)
    merged = await detail(client)
    assert merged["total"] == 2 and merged["books"] == merged["owned"] == 1
    assert {row["work"]["id"] for row in merged["items"]} == {target}
    assert (await client.post(f"/api/identity/changes/{change['id']}/undo")).status_code == 204
    restored = await detail(client)
    assert restored["books"] == 2 and restored["owned"] == 1
    assert restored["items"] == before["items"]


async def test_same_named_series_are_separate_and_terminal_job_is_actionable(
    client, database, service
):
    from sqlalchemy import text

    first = await start(client)
    second = await start(client, "second-series-command", "10")
    await finish(database, first)
    await finish(database, second)
    one, two = await detail(client), await detail(client, "10")
    assert one["name"] == two["name"] and one["id"] != two["id"]
    operation = await start(client, "interrupted-series-command")
    async with database() as db, db.begin():
        op = await db.get(Operation, operation)
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status='failed' WHERE id=:id"),
            {"id": op.job_id},
        )
    interrupted = await detail(client)
    assert interrupted["status"] == "interrupted" and interrupted["items"] == one["items"]
    assert await finish(database, await start(client, "new-observation-command")) == "completed"


async def test_refresh_clicks_with_different_keys_reuse_active_observation(
    client, database, service
):
    first = await start(client, "first-refresh-click")
    assert await start(client, "second-refresh-click") == first
    assert await finish(database, first) == "completed"
    assert len(service.calls) == 4
