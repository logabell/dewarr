# ruff: noqa: F811
"""Bounded projections must not traverse identities outside their catalog scope."""

import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert, select, text

from app.db.models import (
    ImportEntry,
    ImportRun,
    LibraryAsset,
    LibraryReadIssue,
    Operation,
    User,
    Version,
    Work,
)
from app.domain.catalog_display import display_map
from app.domain.work_graph import canonical_families, canonical_map
from tests.integration.test_discovery import add_owned
from tests.integration.test_import_destinations import route  # noqa: F401
from tests.integration.test_list_discovery import books, followed, shelf

pytestmark = pytest.mark.integration


async def test_confirmation_scheduler_batches_operation_reads_without_manifests(
    client,
    admin,
    database,
    route,
    tmp_path,  # noqa: F811
):
    from app.jobs.tasks import schedule_import_confirmation

    payload = {"files": [{"path": f"chapter-{n}.mp3", "metadata": "x" * 250} for n in range(500)]}
    operation_ids, entry_ids = [], []
    async with database() as db, db.begin():
        version_id = await db.scalar(select(Version.id))
        run = ImportRun(
            owner_id=UUID(admin["id"]),
            plan_id=UUID(route["plan_id"]),
            command_key="scheduler-fixture",
            request={},
        )
        db.add(run)
        await db.flush()
        for n in range(19):
            operation = Operation(
                owner_id=UUID(admin["id"]),
                kind="organization.publish",
                idempotency_key=f"confirmation-{n}",
                status="completed",
                payload=payload,
            )
            db.add(operation)
            await db.flush()
            entry = ImportEntry(
                run_id=run.id,
                group_id=uuid4(),
                version_id=version_id,
                destination_id=UUID(route["destination"]["id"]),
                operation_id=operation.id,
                state="awaiting-library",
                message="Waiting for inventory",
                specification=payload,
                configuration=payload,
                receipt=payload,
                expected_metadata=payload,
                published_at=datetime.now(UTC),
                next_check_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            db.add(entry)
            await db.flush()
            operation_ids.append(operation.id)
            entry_ids.append(entry.id)
    calls, loaded = [], []
    engine = database.kw["bind"].sync_engine

    def count(*args):
        calls.append(1)

    def capture(row, context):
        loaded.append({key: value for key, value in vars(row).items() if not key.startswith("_")})

    event.listen(engine, "before_cursor_execute", count)
    for model in (ImportEntry, Operation):
        event.listen(model, "load", capture)
    started = time.perf_counter()
    try:
        await schedule_import_confirmation(0)
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", count)
        for model in (ImportEntry, Operation):
            event.remove(model, "load", capture)
    metrics = {
        "entries": 19,
        "sql_statements": len(calls),
        "orm_materialized_bytes": len(json.dumps(loaded, default=str)),
        "elapsed_seconds": elapsed,
    }
    (tmp_path / "confirmation-scheduler-metrics.json").write_text(json.dumps(metrics))
    async with database() as db:
        operations = list(
            await db.scalars(select(Operation).where(Operation.id.in_(operation_ids)))
        )
        assert len({op.job_id for op in operations}) == 1 and operations[0].job_id is not None
        assert all(op.status == "queued" and op.payload == payload for op in operations)
        entries = list(await db.scalars(select(ImportEntry).where(ImportEntry.id.in_(entry_ids))))
        assert all(
            entry.next_check_at > datetime.now(UTC) and entry.specification == payload
            for entry in entries
        )
    assert len(calls) <= 6, "Confirmation scheduling read operations one at a time"
    assert metrics["orm_materialized_bytes"] < 50000, (
        "Confirmation scheduling loaded publication manifests"
    )


async def test_followed_list_projection_ignores_unrelated_catalog(
    client, admin, database, tmp_path
):
    async with database() as db, db.begin():
        await db.execute(
            insert(Work),
            [
                {"id": uuid4(), "title": f"Unrelated {n}", "authors": ["Other Writer"]}
                for n in range(5000)
            ],
        )
        item, _ = await followed(db, UUID(admin["id"]))
        selected = await books(db, item, 4)
        original_library = await add_owned(db, selected[0])
        ids = [str(work.id) for work in selected[:3]]
        await db.execute(text("ANALYZE works"))
    statements = []
    engine = database.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    started = time.perf_counter()
    try:
        result = await shelf(client)
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", capture)
    card = result["items"][0]
    assert card["count"] == 4 and card["owned"] == 1
    assert [work["id"] for work in card["books"]] == ids
    plans = []
    async with database() as db:
        connection = await db.connection()
        for sql, parameters in statements:
            plans.append(
                (
                    await connection.exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                        parameters,
                    )
                ).scalar_one()[0]
            )

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    metrics = {
        "catalog_works": 5004,
        "list_books": 4,
        "elapsed_seconds": elapsed,
        "sql_statements": len(statements),
        "recursive_rows": sum(
            node["Actual Rows"] * node["Actual Loops"]
            for plan in plans
            for node in nodes(plan["Plan"])
            if node["Node Type"] == "Recursive Union"
        ),
        "execution_ms": sum(plan["Execution Time"] for plan in plans),
        "plans": plans,
    }
    (tmp_path / "list-shelf-metrics.json").write_text(json.dumps(metrics, indent=2))
    assert metrics["recursive_rows"] < 100, (
        "Small followed-list shelf traversed unrelated identities"
    )
    # The only owned representative can be outside the requested lists. Scoping
    # must still include its title family and keep the list's original order.
    async with database() as db, db.begin():
        original_asset = await db.scalar(
            select(LibraryAsset).where(LibraryAsset.library_id == original_library.id)
        )
        original_asset.full_content = False
        outside = Work(title=selected[0].title, authors=selected[0].authors)
        db.add(outside)
        await db.flush()
        await add_owned(db, outside)
    card = (await shelf(client))["items"][0]
    assert card["count"] == 4 and card["owned"] == 1
    assert [work["id"] for work in card["books"]] == [str(outside.id), *ids[1:]]


async def test_review_counts_share_one_asset_scan_and_preserve_overlaps(database, tmp_path):
    from app.domain.library_review import review_counts

    cases = [
        ("needs-review", [], "present"),
        ("matched", ["title"], "present"),
        ("needs-review", ["authors", "description"], "present"),
        ("matched", ["description"], "present"),
        ("needs-review", ["description"], "present"),
        ("needs-review", ["title"], "missing-confirmed"),
        ("needs-review", ["title"], "intentionally-removed"),
        ("matched", [], "present"),
    ]
    async with database() as db, db.begin():
        work = Work(title="Review fixture")
        db.add(work)
        await db.flush()
        library = await add_owned(db, work)
        hidden = await add_owned(db, work)
        hidden.accessible = False
        await db.execute(
            insert(LibraryAsset),
            [
                {
                    "library_id": library.id,
                    "external_id": f"{index}-{n}",
                    "medium": "ebook",
                    "match_status": match,
                    "read_issues": issues,
                    "state": state,
                }
                for index, (match, issues, state) in enumerate(cases)
                for n in range(500)
            ]
            + [
                {
                    "library_id": hidden.id,
                    "external_id": "hidden",
                    "medium": "ebook",
                    "match_status": "needs-review",
                    "read_issues": ["title"],
                    "state": "present",
                }
            ],
        )
        db.add_all(
            [
                LibraryReadIssue(
                    library_id=library.id,
                    external_id=f"issue-{n}",
                    reasons=["Incomplete file"],
                    resolved_at=datetime.now(UTC) if n == 2 else None,
                )
                for n in range(3)
            ]
        )
    calls = []
    engine = database.kw["bind"].sync_engine

    def count(*args):
        calls.append(1)

    async with database() as db:
        event.listen(engine, "before_cursor_execute", count)
        started = time.perf_counter()
        try:
            observed = await review_counts(db, [library.id, hidden.id])
        finally:
            elapsed = time.perf_counter() - started
            event.remove(engine, "before_cursor_execute", count)
        assert observed == {
            "total": 2002,
            "needs_matching": 1500,
            "read_issues": 1002,
            "details": 1000,
        }
        assert await review_counts(db, []) == {
            "total": 0,
            "needs_matching": 0,
            "read_issues": 0,
            "details": 0,
        }
        assert await review_counts(db) == observed
    (tmp_path / "review-count-metrics.json").write_text(
        json.dumps({"assets": 4003, "sql_statements": len(calls), "elapsed_seconds": elapsed})
    )
    assert len(calls) == 2, "Review counters repeatedly scanned the same library assets"


async def test_detail_projection_only_walks_relevant_identity_families(admin, database, tmp_path):
    root, branch, leaf, sibling, recording = [uuid4() for _ in range(5)]
    async with database() as db, db.begin():
        await db.execute(
            insert(Work),
            [{"id": uuid4(), "title": f"Unrelated {index}"} for index in range(5000)]
            + [
                {"id": identifier, "title": "Selected book", "authors": ["Writer"]}
                for identifier in (root, branch, leaf, sibling, recording)
            ],
        )
        for identifier, parent in ((branch, root), (leaf, branch), (sibling, root)):
            (await db.get(Work, identifier)).redirect_to = parent
        await add_owned(db, await db.get(Work, root))
        await db.flush()
        await db.execute(text("ANALYZE works"))
        user = await db.get(User, UUID(admin["id"]))
        mapping = display_map(user, [leaf])
        statement = select(mapping)
        assert dict((await db.execute(statement)).all()) == {
            key: root for key in (root, branch, leaf, sibling, recording)
        }
        sql = str(
            statement.compile(dialect=db.bind.dialect, compile_kwargs={"literal_binds": True})
        )
        connection = await db.connection()
        plan = (
            await connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)
        ).scalar_one()[0]

        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)

        metrics = {
            "catalog_works": 5005,
            "recursive_rows": sum(
                n["Actual Rows"] * n["Actual Loops"]
                for n in nodes(plan["Plan"])
                if n["Node Type"] == "Recursive Union"
            ),
            "plan_rows": sum(n["Actual Rows"] * n["Actual Loops"] for n in nodes(plan["Plan"])),
            "execution_ms": plan["Execution Time"],
            "plan": plan,
        }
        (tmp_path / "detail-metrics.json").write_text(json.dumps(metrics, indent=2))
        assert metrics["recursive_rows"] < 100, "Detail projection walked unrelated identities"

        # Resolve origins upwards, but gather holdings from every descendant of
        # the selected root. A sibling may own the only complete copy.
        assert dict((await db.execute(select(canonical_map([leaf, recording])))).all()) == {
            leaf: root,
            recording: recording,
        }
        assert dict((await db.execute(select(canonical_families([root])))).all()) == {
            key: root for key in (root, branch, leaf, sibling)
        }
        (await db.get(Work, branch)).redirect_to = None
        await db.flush()
        origins = canonical_map(select(Work.id).where(Work.id.in_([leaf, recording])))
        assert dict((await db.execute(select(origins))).all()) == {
            leaf: branch,
            recording: recording,
        }
        assert dict((await db.execute(select(canonical_families([root])))).all()) == {
            root: root,
            sibling: root,
        }
        (await db.get(Work, branch)).redirect_to = leaf
        await db.flush()
        await db.execute(text("SET LOCAL statement_timeout = '1s'"))
        assert dict((await db.execute(select(canonical_map([leaf, root])))).all()) == {root: root}
        assert not (await db.execute(select(canonical_map([])))).all()


async def test_import_history_batches_runs_without_loading_manifests(
    client,
    admin,
    database,
    route,  # noqa: F811
    tmp_path,
):
    owner_id, plan_id = UUID(admin["id"]), UUID(route["plan_id"])
    payload = {"files": [{"path": f"chapter-{n}.mp3", "metadata": "x" * 250} for n in range(500)]}
    async with database() as db, db.begin():
        version_id = await db.scalar(select(Version.id))
        for n in range(25):
            run = ImportRun(
                owner_id=owner_id, plan_id=plan_id, command_key=f"history-{n}", request=payload
            )
            db.add(run)
            await db.flush()
            db.add(
                ImportEntry(
                    run_id=run.id,
                    group_id=uuid4(),
                    version_id=version_id,
                    state="held",
                    message="Review import",
                    reserved=True,
                    specification=payload,
                    configuration={
                        **payload,
                        "destination": {
                            "workflow": "bookdrop",
                            "backend": {"base_url": "http://fixture/"},
                        },
                    },
                    expected_metadata=payload,
                    receipt=payload,
                )
            )
    calls, loaded = [], []
    engine = database.kw["bind"].sync_engine

    def count(*args):
        calls.append(1)

    def capture(row, context):
        loaded.append(
            len(
                json.dumps(
                    {key: value for key, value in vars(row).items() if not key.startswith("_")},
                    default=str,
                )
            )
        )

    event.listen(engine, "before_cursor_execute", count)
    event.listen(ImportEntry, "load", capture)
    event.listen(ImportRun, "load", capture)
    started = time.perf_counter()
    try:
        response = await client.get(f"/api/organization/plans/{plan_id}/imports")
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", count)
        event.remove(ImportEntry, "load", capture)
        event.remove(ImportRun, "load", capture)
    assert response.status_code == 200, response.text
    assert len(response.json()) == 25
    for run in response.json():
        assert len(run["entries"]) == 1
        entry = run["entries"][0]
        assert entry["can_retry"] and entry["can_cancel"]
        assert entry["bookdrop_url"] == "http://fixture/bookdrop"
    metrics = {
        "runs": 25,
        "sql_statements": len(calls),
        "orm_materialized_bytes": sum(loaded),
        "elapsed_seconds": elapsed,
    }
    (tmp_path / "import-history-metrics.json").write_text(json.dumps(metrics))
    assert len(calls) <= 7, "Import history queried entries and restore protection per run"
    assert sum(loaded) < 50000, "Import history loaded full manifests into ORM entities"

    first = response.json()[0]
    entry_id = UUID(first["entries"][0]["id"])
    for specification in ({}, None):
        async with database() as db, db.begin():
            (await db.get(ImportEntry, entry_id)).specification = specification
        result = await client.get(f"/api/organization/imports/{first['id']}")
        assert result.status_code == 200
        assert not result.json()["entries"][0]["can_retry"]
        assert result.json()["entries"][0]["can_cancel"]
    # Restore approval holds must still apply to every run in the batched page.
    from tests.integration.test_recovery_approvals import seal_history

    await seal_history(database, admin)
    held = await client.get(f"/api/organization/plans/{plan_id}/imports")
    assert held.status_code == 200, held.text
    assert len(held.json()) == 25
    for run in held.json():
        entry = run["entries"][0]
        assert not entry["can_retry"] and not entry["can_cancel"]
        assert "predates restore" in entry["message"]
