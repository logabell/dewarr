# ruff: noqa: F401, F811
"""Prerequisite observation must complete before source evidence is frozen."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.db.models import (
    AcquisitionIntent,
    CatalogAccount,
    CatalogSeries,
    Operation,
    User,
    Work,
    WorkMetadataSource,
)
from app.domain import book_sources, pack_coverage
from app.domain import series_preparation as prep
from app.jobs.retry import DependencyRetry
from tests.integration.test_acquisition import catalog
from tests.integration.test_book_sources import begin, read
from tests.integration.test_catalog_series import finish, service
from tests.integration.test_catalog_series import start as refresh
from tests.integration.test_mam_sources import configure as configure_mam
from tests.integration.test_mam_sources import source_http

pytestmark = pytest.mark.integration


@pytest.fixture
async def target(database, catalog):
    async with database() as db, db.begin():
        source = await db.scalar(
            select(WorkMetadataSource).where(WorkMetadataSource.work_id == catalog["work"])
        )
        source.snapshot = {
            **source.snapshot,
            "series": [{"external_id": "9", "name": "Series", "position": "1"}],
        }
    return catalog


async def waiting(database, identifier):
    with pytest.raises(DependencyRetry):
        await prep.run(UUID(identifier))
    async with database() as db:
        operation = await db.get(Operation, UUID(identifier))
        assert operation.payload["workers"] == {}
        return deepcopy(operation.payload["catalog_preparation"])


async def complete(database, identifier):
    stage = await waiting(database, identifier)
    for item in stage["dependencies"]:
        assert await finish(database, UUID(item["operation_id"])) == "completed"
    await prep.run(UUID(identifier))


async def test_hydrates_freezes_and_searches_once_without_authorizing_downloads(
    client, database, admin, service, target, source_http
):
    await configure_mam(client)
    value = await begin(client, target)
    assert value["catalog_preparation"]["state"] == "pending"
    assert value["query_plan"] is None
    await book_sources.run(UUID(value["id"]), "mam")
    assert not source_http["calls"]
    await complete(database, value["id"])
    launched = (await read(client, value["id"])).json()
    assert launched["catalog_preparation"]["state"] == "completed"
    assert not launched["catalog_preparation"]["warnings"]
    assert len(launched["query_plan"]["queries"]) == 2
    await book_sources.run(UUID(value["id"]), "mam")
    assert (await read(client, value["id"])).json()["status"] == "completed"
    assert len(source_http["calls"]) == 2
    await prep.run(UUID(value["id"]))
    async with database() as db:
        user = await db.get(User, UUID(admin["id"]))
        work = await db.get(Work, target["work"])
        context = await pack_coverage.catalog(db, user, work)
        assert len(context["series"]) == 1
        assert len(context["series"][0]["members"]) == 2
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM book_queue.procrastinate_jobs "
                    "WHERE task_name='sources.search'"
                )
            )
            == 1
        )
    assert [call[-1] for call in service.calls] == [0, 1, 0, 1]


async def test_concurrent_searches_share_manual_refresh(client, database, service, target):
    await configure_mam(client)
    manual = await refresh(client)
    searches = [await begin(client, target, key=f"search-{i}") for i in range(2)]
    stages = await asyncio.gather(*(waiting(database, s["id"]) for s in searches))
    assert all(s["dependencies"][0]["operation_id"] == str(manual) for s in stages)
    async with database() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.kind == "catalog.series.refresh")
            )
            == 1
        )
    await finish(database, manual)
    for saved in searches:
        await prep.run(UUID(saved["id"]))
    assert [call[-1] for call in service.calls] == [0, 1, 0, 1]


async def test_concurrent_searches_coalesce_new_observation(client, database, service, target):
    await configure_mam(client)
    searches = [await begin(client, target, key=f"search-{i}") for i in range(2)]
    stages = await asyncio.gather(*(waiting(database, s["id"]) for s in searches))
    assert (
        stages[0]["dependencies"][0]["operation_id"] == stages[1]["dependencies"][0]["operation_id"]
    )


async def test_fresh_catalog_reused_stale_catalog_not_pack_evidence(
    client, database, admin, service, target
):
    await configure_mam(client)
    await finish(database, await refresh(client))
    assert (await begin(client, target))["catalog_preparation"] is None
    async with database() as db, db.begin():
        row = await db.scalar(select(CatalogSeries))
        row.fetched_at = datetime.now(UTC) - timedelta(days=2)
        user = await db.get(User, UUID(admin["id"]))
        work = await db.get(Work, target["work"])
        assert not (await pack_coverage.catalog(db, user, work))["series"]
    stale = await begin(client, target, key="stale-catalog")
    assert stale["catalog_preparation"]["state"] == "pending"
    await complete(database, stale["id"])
    assert [call[-1] for call in service.calls] == [0, 1, 0, 1] * 2


@pytest.mark.parametrize("change", ["disabled", "generation", "deadline"])
async def test_unavailable_prerequisite_falls_back_without_metadata_calls(
    client, database, admin, service, target, source_http, change
):
    await configure_mam(client)
    value = await begin(client, target)
    async with database() as db, db.begin():
        if change == "deadline":
            operation = await db.get(Operation, UUID(value["id"]))
            payload = deepcopy(operation.payload)
            payload["catalog_preparation"]["deadline"] = datetime.now(UTC).isoformat()
            operation.payload = payload
        else:
            account = await db.get(CatalogAccount, UUID(admin["id"]))
            if change == "disabled":
                account.enabled = False
            else:
                account.generation += 1
    await prep.run(UUID(value["id"]))
    result = (await read(client, value["id"])).json()
    assert result["catalog_preparation"]["state"] == "completed"
    assert result["catalog_preparation"]["warnings"]
    await book_sources.run(UUID(value["id"]), "mam")
    assert source_http["calls"] and not service.calls


@pytest.mark.parametrize("change", ["removed-reference", "work-title"])
async def test_identity_changes_stop_preparation(client, database, service, target, change):
    await configure_mam(client)
    value = await begin(client, target)
    async with database() as db, db.begin():
        if change == "work-title":
            (await db.get(Work, target["work"])).title = "Changed"
        else:
            source = await db.scalar(
                select(WorkMetadataSource).where(WorkMetadataSource.work_id == target["work"])
            )
            source.snapshot = {**source.snapshot, "series": []}
    await prep.run(UUID(value["id"]))
    async with database() as db:
        operation = await db.get(Operation, UUID(value["id"]))
        assert operation.payload["catalog_preparation"]["state"] == "failed"
        assert not operation.payload["workers"]
    assert not service.calls


@pytest.mark.parametrize("state", ["failed", "retrying", "cancelled"])
async def test_failed_or_long_cooldown_child_falls_back(client, database, service, target, state):
    await configure_mam(client)
    value = await begin(client, target)
    stage = await waiting(database, value["id"])
    child_id = UUID(stage["dependencies"][0]["operation_id"])
    async with database() as db, db.begin():
        child = await db.get(Operation, child_id)
        child.status, child.message = state, "Fixture metadata failure"
        child.payload = {
            **child.payload,
            "retry_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
    await prep.run(UUID(value["id"]))
    result = (await read(client, value["id"])).json()
    assert result["catalog_preparation"]["warnings"]
    assert result["catalog_preparation"]["items"][0]["state"] == "failed"
    if state in {"failed", "cancelled"}:
        second = await begin(client, target, key="retry-after-failure")
        await prep.run(UUID(second["id"]))
        retry = (await read(client, second["id"])).json()
        assert "cooldown" in retry["catalog_preparation"]["warnings"][0]
        async with database() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Operation)
                    .where(Operation.kind == "catalog.series.refresh")
                )
                == 1
            )


async def test_opt_out_and_disconnected_account_do_not_block(client, database, target):
    await configure_mam(client)
    value = await begin(client, target)
    assert value["catalog_preparation"]["warnings"]
    assert value["query_plan"] is not None
    disabled = await begin(
        client, target, key="no-packs", preference_overrides={"prefer_series_packs": False}
    )
    assert disabled["catalog_preparation"] is None


async def test_stopped_preparation_repairs_visible_status(client, database, service, target):
    await configure_mam(client)
    value = await begin(client, target)
    async with database() as db, db.begin():
        operation = await db.get(Operation, UUID(value["id"]))
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status='failed' WHERE id=:id"),
            {"id": operation.job_id},
        )
    result = (await read(client, value["id"])).json()
    assert result["status"] == "completed"
    assert result["catalog_preparation"]["state"] == "failed"
    assert "stopped" in result["catalog_preparation"]["message"]


async def test_real_worker_resumes_preparation_before_source_jobs(
    client, database, service, target, source_http, monkeypatch
):
    from app.jobs.queue import get_queue

    # Exercise a finite dependency graph independently of wall-clock periodic jobs.
    monkeypatch.setattr(get_queue().periodic_registry, "periodic_tasks", {})
    await configure_mam(client)
    value = await begin(client, target)
    for _ in range(8):
        await asyncio.wait_for(
            get_queue().run_worker_async(
                wait=False, concurrency=1, listen_notify=False, install_signal_handlers=False
            ),
            15,
        )
    async with database() as db, db.begin():
        operation = await db.get(Operation, UUID(value["id"]))
        assert operation.payload["catalog_preparation"]["state"] == "waiting"
        assert not operation.payload["workers"]
        child = await db.get(
            Operation,
            UUID(operation.payload["catalog_preparation"]["dependencies"][0]["operation_id"]),
        )
        assert child.status == "completed"
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET scheduled_at=now() WHERE id=:id"),
            {"id": operation.job_id},
        )
    assert not source_http["calls"]
    for _ in range(3):
        await asyncio.wait_for(
            get_queue().run_worker_async(
                wait=False, concurrency=1, listen_notify=False, install_signal_handlers=False
            ),
            15,
        )
    result = (await read(client, value["id"])).json()
    assert result["status"] == "completed", result
    assert result["catalog_preparation"]["state"] == "completed"
    assert len(source_http["calls"]) == 2
    assert [call[-1] for call in service.calls] == [0, 1, 0, 1]


async def test_source_configuration_generation_remains_frozen(
    client, database, service, target, source_http
):
    from app.db.models import SourceConnection

    await configure_mam(client)
    value = await begin(client, target)
    await complete(database, value["id"])
    async with database() as db, db.begin():
        (await db.get(SourceConnection, "mam")).generation += 1
    await book_sources.run(UUID(value["id"]), "mam")
    result = (await read(client, value["id"])).json()
    assert all(item["state"] == "failed" for item in result["sources"])
    assert not source_http["calls"]
