"""Opt-in production-query benchmark; run only through scripts/check.py.

BOOK_RUN_LIBRARY_BENCHMARK=1 BOOK_BENCHMARK_LABEL=before
BOOK_TEST_DATABASE_URL=postgresql+psycopg:///library_benchmark_test
python3 scripts/check.py backend tests/integration/test_library_benchmark.py
"""

import asyncio
import json
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event, insert, select, text, update

from app.db.models import (
    AssetContains,
    Integration,
    InventoryItemState,
    Library,
    LibraryAsset,
    LibraryGrant,
    Operation,
    Version,
    Work,
)
from app.db.session import get_engine
from app.security import encrypt_secrets

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("BOOK_RUN_LIBRARY_BENCHMARK") != "1", reason="Opt-in benchmark"),
]


def save_report(kind, report):
    folder = Path(".local/benchmarks")
    folder.mkdir(parents=True, exist_ok=True)
    label = os.getenv("BOOK_BENCHMARK_LABEL", "latest")
    assert label.replace("-", "").isalnum()
    (folder / f"{kind}-10000-{label}.json").write_text(json.dumps(report, indent=2))


async def seed(database):
    async with database() as db, db.begin():
        backend = Integration(
            kind="audiobookshelf",
            name="Benchmark",
            base_url="http://benchmark.invalid",
            encrypted_secrets=encrypt_secrets({"token": "synthetic"}),
        )
        db.add(backend)
        await db.flush()
        library = Library(integration_id=backend.id, external_id="benchmark", name="Benchmark")
        db.add(library)
        await db.flush()
        library_id = library.id
    now = datetime.now(UTC)
    for start in range(0, 10_000, 500):
        works, versions, assets, contains = [], [], [], []
        for n in range(start, start + 500):
            number = n - 1 if n % 10 == 9 else n
            title, medium = f"Benchmark Book {number:05}", "audio" if n % 2 else "ebook"
            work_id, version_id, asset_id = (UUID(int=n + base) for base in (1000, 101000, 201000))
            works.append(
                dict(id=work_id, title=title, authors=[f"Author {number // 20}"], language="en")
            )
            versions.append(dict(id=version_id, work_id=work_id, medium=medium))
            assets.append(
                dict(
                    id=asset_id,
                    library_id=library_id,
                    external_id=str(n),
                    version_id=version_id,
                    title=title,
                    medium=medium,
                    state="present",
                    full_content=True,
                    last_seen_at=now,
                    metadata_snapshot={
                        "narrators": ["Reader"],
                        "cover_path": "cover.jpg",
                        "description": "text " * 400,
                    },
                    files=[
                        {
                            "path": f"/synthetic/{n}/track-{i}.mp3",
                            "format": "mp3",
                            "size": 1_000_000,
                        }
                        for i in range(12)
                    ],
                )
            )
            contains.append(dict(asset_id=asset_id, work_id=work_id, verified=True))
        async with database() as db, db.begin():
            for model, rows in (
                (Work, works),
                (Version, versions),
                (LibraryAsset, assets),
                (AssetContains, contains),
            ):
                await db.execute(insert(model), rows)
    async with database() as db, db.begin():
        for table in (
            "works",
            "versions",
            "library_assets",
            "asset_contains",
            "libraries",
            "integrations",
        ):
            await db.execute(text(f"ANALYZE {table}"))
    return backend.id, library_id


async def test_ten_thousand_book_pages(client, admin, database, monkeypatch):
    await seed(database)

    async def cover(*args, **kwargs):
        return b"synthetic-image", "image/jpeg"

    monkeypatch.setattr("app.domain.library_covers.fetch_cover", cover)
    report = {"books": 10_000, "platform": platform.platform(), "requests": []}
    engine = get_engine().sync_engine
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        context.benchmark_start = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        statements.append(
            {"ms": (time.perf_counter() - context.benchmark_start) * 1000, "sql": statement[:1400]}
        )

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        for path in (
            "/api/library/books?limit=40",
            "/api/library/books?limit=40&offset=8000",
            "/api/catalog/works?limit=40&q=Book%200001",
            "/api/discovery/library",
            f"/api/catalog/works/{UUID(int=1000)}/cover",
        ):
            for attempt in range(2):
                statements.clear()
                started = time.perf_counter()
                response = await client.get(path)
                measurement = {
                    "path": path,
                    "attempt": attempt,
                    "ms": (time.perf_counter() - started) * 1000,
                    "status": response.status_code,
                    "sql_count": len(statements),
                    "sql_ms": sum(row["ms"] for row in statements),
                    "slowest": sorted(statements, key=lambda row: row["ms"], reverse=True)[:3],
                }
                report["requests"].append(measurement)
                assert response.status_code == 200, response.text
                if "/library/books" in path:
                    assert response.json()["total"] == 9000
                    assert len(response.json()["items"]) == 40
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
        await asyncio.to_thread(save_report, "library", report)


async def test_ten_thousand_unchanged_inventory(client, admin, database):
    from app.adapters.audiobookshelf import Audiobookshelf
    from app.adapters.contracts import Capabilities
    from app.domain.inventory import INVENTORY_SCHEMA, summary_fingerprint, synchronize

    integration_id, library_id = await seed(database)
    counts = {"pages": 0, "details": 0}

    def summaries(page):
        return [
            {
                "id": str(n),
                "updatedAt": 1,
                "path": f"/synthetic/{n}",
                "media": {"metadata": {"title": f"Book {n}"}},
            }
            for n in range(page * 100, min((page + 1) * 100, 10_000))
        ]

    class CachedLibrary(Audiobookshelf):
        async def authorize(self):
            return Capabilities(operations={"inventory"}), "benchmark"

        async def libraries(self):
            return [{"id": "benchmark", "name": "Benchmark"}]

        async def page(self, library_id, page):
            counts["pages"] += 1
            return summaries(page), 10_000

        async def inventory_items(self, library_id, records):
            assert not records, "An unchanged scan must not fetch expanded metadata"
            if records:
                counts["details"] += len(records)
                yield None

    async with database() as db, db.begin():
        (await db.get(Library, library_id)).scope_fingerprint = "benchmark"
        for page in range(100):
            await db.execute(
                insert(InventoryItemState),
                [
                    dict(
                        integration_id=integration_id,
                        library_external_id="benchmark",
                        item_external_id=key,
                        source_marker=list(marker),
                        observed_media=["audio" if int(key) % 2 else "ebook"],
                        credential_generation=0,
                        scope_fingerprint="benchmark",
                        schema_version=INVENTORY_SCHEMA,
                        checked_at=datetime.now(UTC),
                    )
                    for key, marker in summary_fingerprint(summaries(page)).items()
                ],
            )
        operation = Operation(
            owner_id=UUID(admin["id"]),
            kind="library.sync",
            integration_id=integration_id,
            idempotency_key="benchmark-sync",
        )
        db.add(operation)
        await db.flush()
        operation_id = operation.id
    statements = {"all": 0, "asset_updates": 0}

    def after(conn, cursor, statement, parameters, context, executemany):
        statements["all"] += 1
        statements["asset_updates"] += statement.startswith("UPDATE library_assets")

    engine = get_engine().sync_engine
    event.listen(engine, "after_cursor_execute", after)
    started = time.perf_counter()
    reader_ms = []

    async def browse_during_scan():
        await asyncio.sleep(0.1)
        for _ in range(2):
            before = time.perf_counter()
            response = await client.get("/api/library/books?limit=40")
            reader_ms.append((time.perf_counter() - before) * 1000)
            assert response.status_code == 200 and response.json()["total"] == 9000

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(synchronize(operation_id, client_factory=CachedLibrary))
            if os.getenv("BOOK_BENCHMARK_WITH_READER") == "1":
                tasks.create_task(browse_during_scan())
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        event.remove(engine, "after_cursor_execute", after)
    async with database() as db:
        operation = await db.get(Operation, operation_id)
        assert operation.status == "completed", operation.message
        assert operation.payload["inventory"] == {
            "items": 10_000,
            "details_read": 0,
            "details_reused": 10_000,
        }
        assert (await db.scalars(select(LibraryAsset.seen_generation).distinct())).all() == [1]
    await asyncio.to_thread(
        save_report,
        "inventory",
        {
            "books": 10_000,
            "ms": elapsed,
            "source_calls": counts,
            "sql": statements,
            "reader_ms": reader_ms,
        },
    )


async def test_ten_thousand_private_member_pages(client, admin, database):
    from tests.integration.test_discovery import login_member

    _, library_id = await seed(database)
    member_id = await login_member(client)
    async with database() as db, db.begin():
        await db.execute(update(Work).values(catalog_public=False))
        db.add(LibraryGrant(library_id=library_id, user_id=UUID(str(member_id))))
    results = []
    for path in ("/api/library/books?limit=40", "/api/library/books?limit=40&offset=8000"):
        for _ in range(2):
            started = time.perf_counter()
            response = await client.get(path)
            results.append({"path": path, "ms": (time.perf_counter() - started) * 1000})
            assert response.status_code == 200, response.text
            assert response.json()["total"] == 9000 and len(response.json()["items"]) == 40
    await asyncio.to_thread(save_report, "member", results)


async def test_prepared_title_family_lookup(client, admin, database):
    from app.domain.catalog_titles import display_base_sql

    await seed(database)
    times = []
    async with database() as db:
        await db.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        query = select(Work.id).where(display_base_sql(Work.title) == "benchmark book 00000")
        for _ in range(15):
            started = time.perf_counter()
            assert len((await db.scalars(query)).all()) == 1
            times.append((time.perf_counter() - started) * 1000)
        plans = (
            await db.execute(
                text(
                    "SELECT generic_plans, custom_plans FROM pg_prepared_statements "
                    "WHERE statement LIKE 'SELECT works.id%FROM works%split_part%'"
                )
            )
        ).all()
        assert plans and any(row.generic_plans for row in plans)
        compiled = query.compile(
            dialect=get_engine().dialect,
            compile_kwargs={"render_postcompile": True},
        )
        assert len(compiled.params) == 1  # Only the requested title family is variable.
        statement = str(compiled).replace(f"%({next(iter(compiled.params))})s", "$1")
        connection = await db.connection()
        plan = (
            await connection.exec_driver_sql("EXPLAIN (GENERIC_PLAN, FORMAT JSON) " + statement)
        ).scalar_one()
        assert "ix_works_display_base" in json.dumps(plan)
    await asyncio.to_thread(save_report, "prepared", {"ms": times, "plan": plan})
