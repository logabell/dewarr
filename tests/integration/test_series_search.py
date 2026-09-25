# ruff: noqa: F401, F811
import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.adapters.contracts import AdapterError, FailureKind
from app.db.models import (
    AcquisitionIntent,
    CatalogSeries,
    Operation,
    SeriesMembership,
    SourceResult,
    User,
    Work,
    WorkMetadataSource,
)
from app.domain import book_sources, source_queries
from app.jobs.retry import SourceSearchRetry
from tests.integration.test_acquisition import catalog
from tests.integration.test_acquisition_defaults import save as defaults
from tests.integration.test_book_sources import begin, read
from tests.integration.test_correction_migration import migrate
from tests.integration.test_mam_sources import configure as configure_mam
from tests.integration.test_mam_sources import source_http
from tests.integration.test_prowlarr_sources import configure as configure_prowlarr
from tests.integration.test_prowlarr_sources import prowlarr_http

pytestmark = pytest.mark.integration


async def names(database, catalog, values):
    async with database() as db, db.begin():
        source = await db.scalar(
            select(WorkMetadataSource).where(WorkMetadataSource.work_id == catalog["work"])
        )
        source.snapshot = {
            **source.snapshot,
            "series": [
                {"external_id": str(i), "name": name, "position": "1", "compilation": False}
                for i, name in enumerate(values)
            ],
        }


@pytest.fixture
async def series(database, catalog):
    await names(database, catalog, ["Harbor Stories", "Harbor Cycle"])
    return catalog


async def test_title_and_series_queries_merge_results_and_keep_provenance(
    client, database, series, source_http, prowlarr_http
):
    await configure_mam(client)
    await configure_prowlarr(client)
    saved = await begin(client, series)
    assert [q["query"] for q in saved["query_plan"]["queries"]] == [
        "Harbor",
        "Harbor Cycle",
        "Harbor Stories",
    ]
    for source in ("mam", "prowlarr"):
        await book_sources.run(UUID(saved["id"]), source)
    result = (await read(client, saved["id"])).json()
    assert result["status"] == "completed"
    assert len(result["items"]) == 2
    assert all(r["query_keys"] == ["book", "series:0", "series:1"] for r in result["items"])
    assert {json.loads(r.content)["tor"]["text"] for r in source_http["calls"]} == {
        "Harbor",
        "Harbor Cycle",
        "Harbor Stories",
    }
    assert {
        r.url.params["query"] for r in prowlarr_http["calls"] if r.url.path.endswith("/search")
    } == {"Harbor", "Harbor Cycle", "Harbor Stories"}
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(SourceResult)) == 2
        assert await db.scalar(select(func.count()).select_from(AcquisitionIntent)) == 0
    await book_sources.run(UUID(saved["id"]), "mam")
    assert len(source_http["calls"]) == 3
    assert (await begin(client, series))["id"] == saved["id"]


async def test_query_plan_is_bounded_deduplicated_and_skips_unsupported_names(
    client, database, catalog
):
    await names(
        database,
        catalog,
        ["Harbor", "  Harbor   Stories ", "harbor stories", "A", "B", "C", "D", "x" * 301],
    )
    saved = await begin(client, catalog)
    terms = saved["query_plan"]["queries"]
    assert [q["query"] for q in terms] == ["Harbor", "A", "B", "C"]
    assert len(saved["query_plan"]["warnings"]) == 2
    await names(database, catalog, ["  Harbor   Stories ", "harbor stories"])
    deduped = await begin(client, catalog, key="same-series-names")
    assert len(deduped["query_plan"]["queries"]) == 2
    assert len(deduped["query_plan"]["queries"][1]["evidence"]) == 2


async def test_inherited_series_preference_and_request_override(
    client, database, series, source_http
):
    await configure_mam(client)
    await defaults(client, {"search_series": False})
    title_only = await begin(client, series)
    assert len(title_only["query_plan"]["queries"]) == 1
    assert title_only["profile"]["origins"]["search_series"] == "Personal default"
    override = await begin(
        client, series, key="search-override", preference_overrides={"search_series": True}
    )
    assert len(override["query_plan"]["queries"]) == 3
    assert override["profile"]["origins"]["search_series"] == "Request override"
    await defaults(client, {"search_series": True})
    await book_sources.run(UUID(title_only["id"]), "mam")
    assert len(source_http["calls"]) == 1  # Already saved search scope is not expanded.


@pytest.mark.parametrize("failure", [FailureKind.RATE_LIMIT, FailureKind.UNAVAILABLE])
async def test_independent_queries_resume_without_repeating_completed_results(
    client, database, series, source_http, monkeypatch, failure
):
    await configure_mam(client)
    actual = book_sources.source_call
    calls = []

    async def remote(owner, action, query, **kwargs):
        calls.append(query.q)
        if len(calls) == 2:
            raise AdapterError(failure, "Query unavailable", retry_after=1)
        return await actual(owner, action, query, **kwargs)

    monkeypatch.setattr(book_sources, "source_call", remote)
    saved = await begin(client, series)
    if failure == FailureKind.RATE_LIMIT:
        with pytest.raises(SourceSearchRetry):
            await book_sources.run(UUID(saved["id"]), "mam")
        partial = (await read(client, saved["id"])).json()
        assert len(partial["items"]) == 1 and partial["sources"][0]["state"] == "completed"
        await book_sources.run(UUID(saved["id"]), "mam")
        assert calls == ["Harbor", "Harbor Cycle", "Harbor Cycle", "Harbor Stories"]
    else:
        await book_sources.run(UUID(saved["id"]), "mam")
        assert calls == ["Harbor", "Harbor Cycle", "Harbor Stories"]
    result = (await read(client, saved["id"])).json()
    assert result["status"] == "completed" and len(result["items"]) == 1
    assert result["items"][0]["query_keys"] == (
        ["book", "series:0", "series:1"]
        if failure == FailureKind.RATE_LIMIT
        else ["book", "series:1"]
    )


async def test_changed_series_evidence_fences_inflight_result_and_redacts_queries(
    client, database, series, source_http
):
    await configure_mam(client)
    saved = await begin(client, series)
    source_http["wait"] = asyncio.Event()
    task = asyncio.create_task(book_sources.run(UUID(saved["id"]), "mam"))
    await asyncio.wait_for(source_http["entered"].wait(), 5)
    await names(database, series, ["Changed Series"])
    source_http["wait"].set()
    await task
    result = (await read(client, saved["id"])).json()
    assert result["stale_identity"] and result["query_plan"] is None
    assert not result["items"] and all(s["query"] is None for s in result["sources"])
    assert all(s["state"] == "failed" for s in result["sources"])
    assert len(source_http["calls"]) == 1


async def test_private_series_and_private_merged_origin_do_not_supply_terms(
    database, admin, series
):
    async with database() as db, db.begin():
        owner = await db.get(User, UUID(admin["id"]))
        other = User(
            username="private-series-user",
            display_name="Other",
            password_hash="unused",
            role="member",
        )
        db.add(other)
        await db.flush()
        hidden = Work(
            title="Hidden source",
            authors=[],
            catalog_public=False,
            catalog_owner_id=other.id,
            redirect_to=series["work"],
        )
        private = CatalogSeries(
            owner_id=other.id,
            provider="hardcover",
            external_id="secret",
            name="Private series",
            fetched_at=datetime.now(UTC),
        )
        own = CatalogSeries(
            owner_id=owner.id,
            provider="hardcover",
            external_id="shared-work",
            name="Observed Cycle",
            fetched_at=datetime.now(UTC),
        )
        db.add_all([hidden, private, own])
        await db.flush()
        db.add(
            WorkMetadataSource(
                work_id=hidden.id,
                provider="hardcover",
                external_id="private",
                fetched_at=datetime.now(UTC),
                snapshot={"series": [{"external_id": "hidden", "name": "Hidden Origin"}]},
            )
        )
        for row in (private, own):
            db.add(
                SeriesMembership(
                    series_id=row.id,
                    external_id="1",
                    work_id=series["work"],
                    snapshot={},
                    present=True,
                )
            )
        await db.flush()
        owner.role = "member"
        work = await db.get(Work, series["work"])
        plan = await source_queries.plan(db, owner, work, work.title, True)
        assert {q["query"] for q in plan["queries"]} == {
            "Harbor",
            "Harbor Cycle",
            "Harbor Stories",
            "Observed Cycle",
        }
        assert "Private series" not in str(plan) and "Hidden Origin" not in str(plan)


async def test_legacy_search_retains_single_query_behavior(client, database, series, source_http):
    await configure_mam(client)
    saved = await begin(client, series)
    async with database() as db, db.begin():
        op = await db.get(Operation, UUID(saved["id"]))
        payload = deepcopy(op.payload)
        payload.pop("query_plan")
        payload["sources"] = {"mam": payload["sources"]["mam"]}
        payload["sources"]["mam"].pop("query_key")
        payload["sources"]["mam"].pop("query")
        op.payload = payload
    await book_sources.run(UUID(saved["id"]), "mam")
    assert len(source_http["calls"]) == 1
    result = (await read(client, saved["id"])).json()
    assert result["query_plan"] is None and len(result["items"]) == 1


async def test_populated_query_history_blocks_lossy_downgrade(client, database, series):
    await begin(client, series)
    result = await migrate("downgrade", "0034_series_requests")
    assert (
        result.returncode != 0
        and "Source query history requires a pre-upgrade backup" in result.stderr
    )


async def test_unchanged_metadata_refresh_keeps_frozen_query_evidence(
    client, database, series, source_http
):
    await configure_mam(client)
    saved = await begin(client, series)
    async with database() as db, db.begin():
        source = await db.scalar(
            select(WorkMetadataSource).where(WorkMetadataSource.work_id == series["work"])
        )
        source.fetched_at = datetime.now(UTC)
    await book_sources.run(UUID(saved["id"]), "mam")
    result = (await read(client, saved["id"])).json()
    assert not result["stale_identity"] and len(result["items"]) == 1
    assert result["query_plan"] == saved["query_plan"]


async def test_prowlarr_series_retry_keeps_title_and_same_indexer_deduplicated(
    client, database, series, prowlarr_http, monkeypatch
):
    await configure_prowlarr(client)
    actual = book_sources.prowlarr_call
    calls = []
    discoveries = []

    async def remote(owner, action, query=None, **kwargs):
        if action == "search":
            calls.append(query.q)
            if len(calls) == 2:
                raise AdapterError(FailureKind.RATE_LIMIT, "Wait for this query", retry_after=1)
            return await actual(owner, action, query, **kwargs)
        discoveries.append(action)
        return await actual(owner, action, **kwargs)

    monkeypatch.setattr(book_sources, "prowlarr_call", remote)
    saved = await begin(client, series)
    with pytest.raises(SourceSearchRetry):
        await book_sources.run(UUID(saved["id"]), "prowlarr")
    await book_sources.run(UUID(saved["id"]), "prowlarr")
    assert calls == ["Harbor", "Harbor Cycle", "Harbor Cycle", "Harbor Stories"]
    result = (await read(client, saved["id"])).json()
    assert len(result["items"]) == 1 and result["items"][0]["query_keys"] == [
        "book",
        "series:0",
        "series:1",
    ]
    assert discoveries == ["indexers"]
    # Every actual search also revalidates indexer capabilities at the adapter boundary.
    assert len([r for r in prowlarr_http["calls"] if r.url.path.endswith("/indexer")]) == 4
