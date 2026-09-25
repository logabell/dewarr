from datetime import UTC, datetime, timedelta
from uuid import UUID

from procrastinate import RetryStrategy
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AuditEvent, Integration, Operation, User
from app.db.session import session_factory
from app.jobs.queue import tasks
from app.jobs.retry import (
    CatalogRetryStrategy,
    DependencyRetryStrategy,
    ShelfRetryStrategy,
    SourceSearchRetryStrategy,
)


@tasks.task(
    name="organization.publish", queue="imports", retry=RetryStrategy(max_attempts=4, wait=30)
)
async def publish_book(operation_id: str) -> None:
    from app.importing.execution import execute

    await execute(UUID(operation_id))


@tasks.periodic(cron="* * * * *")
@tasks.task(name="organization.confirm", queue="imports", retry=3)
async def schedule_import_confirmation(timestamp: int) -> None:
    from sqlalchemy import text

    from app.db.models import ImportEntry
    from app.jobs.queue import enqueue

    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        entries = (
            await db.scalars(
                select(ImportEntry)
                .where(
                    ImportEntry.state.in_(["awaiting-library", "cancelling", "queued"]),
                    ImportEntry.next_check_at <= datetime.now(UTC),
                )
                .order_by(ImportEntry.next_check_at, ImportEntry.id)
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for entry in entries:
            operation = await db.get(Operation, entry.operation_id)
            status = await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": operation.job_id},
            )
            if status in {"todo", "doing"}:
                continue
            operation.status = "queued"
            operation.job_id = await enqueue(
                db, "organization.publish", operation_id=str(operation.id)
            )
            entry.next_check_at = datetime.now(UTC) + timedelta(minutes=1)


@tasks.task(name="organization.probe", queue="inspection", retry=3)
async def check_destination(operation_id: str) -> None:
    from app.importing.destinations import probe_route

    await probe_route(UUID(operation_id))


@tasks.task(name="organization.inspect", queue="inspection", retry=3)
async def inspect_completed_download(operation_id: str) -> None:
    from app.importing.workflow import run_inspection

    await run_inspection(UUID(operation_id))


@tasks.task(name="system.probe", queue="system", retry=3)
async def system_probe(operation_id: str) -> None:
    async with session_factory()() as db, db.begin():
        operation = await db.scalar(
            select(Operation).where(Operation.id == UUID(operation_id)).with_for_update()
        )
        if not operation or operation.status == "completed":
            return
        operation.status = "completed"
        operation.message = "The worker received and completed the persistent job"
        db.add(
            AuditEvent(
                actor_id=operation.owner_id,
                action="system.probe.completed",
                entity_id=operation.id,
            )
        )


@tasks.task(name="library.sync", queue="inventory", retry=RetryStrategy(max_attempts=5, wait=60))
async def library_sync(operation_id: str) -> None:
    from app.domain.inventory import synchronize

    await synchronize(UUID(operation_id))


@tasks.task(
    name="metadata.enrich", queue="metadata", retry=CatalogRetryStrategy(max_attempts=5, wait=60)
)
async def enrich_metadata(operation_id: str) -> None:
    from app.domain.catalog_enrichment import enrich

    await enrich(UUID(operation_id))


@tasks.task(
    name="library.match", queue="metadata", retry=CatalogRetryStrategy(max_attempts=3, wait=60)
)
async def match_library_books(operation_id: str) -> None:
    from app.domain.library_matching import match_library

    await match_library(UUID(operation_id))


@tasks.task(name="library.combine", queue="imports", retry=RetryStrategy(max_attempts=3, wait=60))
async def combine_library_parts(operation_id: str) -> None:
    from app.importing.combine import run

    await run(UUID(operation_id))


@tasks.task(
    name="catalog.series.refresh",
    queue="metadata",
    retry=CatalogRetryStrategy(max_attempts=5, wait=60),
)
async def refresh_series(operation_id: str) -> None:
    from app.domain.catalog_series import run

    await run(UUID(operation_id))


@tasks.task(
    name="metadata.resolve-import",
    queue="metadata",
    retry=CatalogRetryStrategy(max_attempts=5, wait=60),
)
async def resolve_import_metadata(operation_id: str) -> None:
    from app.importing.catalog_resolution import resolve

    await resolve(UUID(operation_id))


@tasks.task(name="acquisition.evaluate", queue="acquisition", retry=3)
async def evaluate_acquisition(operation_id: str) -> None:
    if get_settings().recovery_mode:
        raise RuntimeError("Request evaluation is paused for recovery")
    from app.db.models import AcquisitionIntent
    from app.domain.acquisition import evaluate
    from app.domain.work_graph import acquisition_lock

    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, UUID(operation_id))
        if (
            not operation
            or operation.kind != "acquisition.evaluate"
            or operation.status == "completed"
        ):
            return
        intent = await db.get(AcquisitionIntent, UUID(operation.payload["intent_id"]))
        await acquisition_lock(db, intent.work_id)
        await db.refresh(operation, with_for_update=True)
        if operation.status == "completed":
            return
        user = await db.get(User, intent.owner_id)
        await evaluate(db, user, intent)
        operation.status, operation.message = (
            "completed",
            "Wanted media rechecked against your library",
        )


@tasks.periodic(cron="*/5 * * * *")
@tasks.task(name="acquisition.reconcile", queue="acquisition", retry=3)
async def reconcile_acquisition(timestamp: int) -> None:
    from app.domain.acquisition import reconcile_requests

    await reconcile_requests()


@tasks.periodic(cron="*/5 * * * *")
@tasks.task(name="library.schedule", queue="system", retry=3)
async def schedule_inventory(timestamp: int) -> None:
    if get_settings().recovery_mode:
        return
    from app.domain.operations import enqueue_sync

    async with session_factory()() as db, db.begin():
        admin = await db.scalar(
            select(User)
            .where(User.role == "admin", User.active.is_(True))
            .order_by(User.created_at)
            .limit(1)
        )
        if not admin:
            return
        records = (
            await db.scalars(
                select(Integration)
                .where(
                    Integration.kind.in_(["audiobookshelf", "grimmory"]),
                    Integration.enabled.is_(True),
                    Integration.next_sync_at <= datetime.now(UTC),
                )
                .order_by(Integration.id)
                .limit(20)
            )
        ).all()
        for record in records:
            await enqueue_sync(db, admin.id, record.id, f"inventory:{record.id}:{timestamp}")
            record.next_sync_at = datetime.now(UTC) + timedelta(minutes=30)


@tasks.task(name="acquisition.download", queue="acquisition", retry=3)
async def download_attempt(attempt_id: str) -> None:
    from app.domain.download_attempts import run

    await run(UUID(attempt_id))


@tasks.task(name="organization.automatic", queue="imports", retry=3)
async def automatic_import(automatic_id: str) -> None:
    from app.importing.automatic import run

    await run(UUID(automatic_id))


@tasks.task(name="acquisition.fulfillment", queue="acquisition", retry=3)
async def reconcile_fulfillment(work_id: str) -> None:
    from app.domain.download_fulfillment import reconcile_work

    async with session_factory()() as db, db.begin():
        await reconcile_work(db, UUID(work_id))


@tasks.periodic(cron="* * * * *")
@tasks.task(name="acquisition.downloads.schedule", queue="acquisition", retry=3)
async def schedule_downloads(timestamp: int) -> None:
    from sqlalchemy import or_, text

    from app.db.models import AutomaticImport, AutomaticImportContinuation, DownloadAttempt
    from app.importing.automatic import recover
    from app.importing.reuse import recover as recover_reuse
    from app.jobs.queue import enqueue

    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        now = datetime.now(UTC)
        rows = await db.scalars(
            select(DownloadAttempt)
            .where(
                DownloadAttempt.state.not_in(["complete", "cancelled"]),
                DownloadAttempt.next_check_at <= now,
                or_(DownloadAttempt.lease_until.is_(None), DownloadAttempt.lease_until <= now),
            )
            .order_by(DownloadAttempt.next_check_at, DownloadAttempt.id)
            .limit(20)
            .with_for_update(skip_locked=True)
        )
        for row in rows:
            operation = await db.get(Operation, row.operation_id)
            status = await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": operation.job_id},
            )
            if status in {"todo", "doing"}:
                continue
            operation.job_id = await enqueue(db, "acquisition.download", attempt_id=str(row.id))
            row.next_check_at = now + timedelta(minutes=1)
        automatic_rows = await db.scalars(
            select(AutomaticImport)
            .where(AutomaticImport.state.in_(["queued", "inspecting"]))
            .order_by(AutomaticImport.created_at)
            .limit(20)
        )
        for automatic in automatic_rows:
            await recover(db, automatic.id)
        continuations = await db.scalars(
            select(AutomaticImportContinuation)
            .where(AutomaticImportContinuation.state.in_(["queued", "inspecting"]))
            .order_by(AutomaticImportContinuation.created_at, AutomaticImportContinuation.id)
            .limit(20)
        )
        for continuation in continuations:
            await recover_reuse(db, continuation.id)
    from app.domain.series_acquisition import schedule as schedule_series

    await schedule_series()
    from app.domain.download_recovery import schedule as schedule_recovery

    await schedule_recovery()


@tasks.task(
    name="sources.search", queue="sources", retry=SourceSearchRetryStrategy(max_attempts=5, wait=10)
)
async def search_book_sources(operation_id: str, source: str) -> None:
    from app.domain.book_sources import run

    await run(UUID(operation_id), source)


@tasks.task(name="lists.sync", queue="lists", retry=ShelfRetryStrategy(max_attempts=5, wait=60))
async def observe_shelf(operation_id: str) -> None:
    from app.domain.list_subscriptions import run

    await run(UUID(operation_id))


@tasks.task(
    name="lists.writeback", queue="lists", retry=ShelfRetryStrategy(max_attempts=5, wait=60)
)
async def write_list_membership(operation_id: str) -> None:
    from app.domain.list_writeback import run

    await run(UUID(operation_id))


@tasks.task(
    name="lists.writeback.compare", queue="lists", retry=ShelfRetryStrategy(max_attempts=5, wait=60)
)
async def compare_list_membership(operation_id: str) -> None:
    from app.domain.list_comparisons import run

    await run(UUID(operation_id))


@tasks.periodic(cron="* * * * *")
@tasks.task(name="lists.schedule", queue="lists", retry=3)
async def schedule_shelves(timestamp: int) -> None:
    from app.domain.list_subscriptions import schedule

    await schedule()


@tasks.task(name="lists.csv", queue="lists", retry=RetryStrategy(max_attempts=3, wait=10))
async def import_csv(operation_id: str) -> None:
    from app.domain.list_csv import run

    await run(UUID(operation_id))


@tasks.task(name="lists.requests", queue="lists", retry=RetryStrategy(max_attempts=3, wait=10))
async def request_list_books(operation_id: str) -> None:
    from app.domain.list_requests import run

    await run(UUID(operation_id))


@tasks.task(
    name="acquisition.auto-select",
    queue="sources",
    retry=SourceSearchRetryStrategy(max_attempts=5, wait=30),
)
async def select_best_release(operation_id: str) -> None:
    from app.domain.automatic_selection import run

    await run(UUID(operation_id))


@tasks.periodic(cron="* * * * *")
@tasks.task(name="lists.acquisition.schedule", queue="lists", retry=3)
async def schedule_list_acquisition(timestamp: int) -> None:
    from app.domain.list_automation import schedule

    await schedule()


@tasks.task(name="lists.acquire", queue="lists", retry=3)
async def acquire_list_books(operation_id: str) -> None:
    from app.domain.list_automation import run

    await run(UUID(operation_id))


@tasks.task(name="series.requests", queue="metadata", retry=RetryStrategy(max_attempts=5, wait=60))
async def request_series(operation_id: str) -> None:
    from app.domain.series_requests import run

    await run(UUID(operation_id))


@tasks.task(
    name="series.acquire", queue="acquisition", retry=RetryStrategy(max_attempts=3, wait=30)
)
async def acquire_series(operation_id: str) -> None:
    from app.domain.series_acquisition import run

    await run(UUID(operation_id))


@tasks.task(
    name="sources.prepare", queue="metadata", retry=DependencyRetryStrategy(max_attempts=3, wait=10)
)
async def prepare_search_catalog(search_id: str) -> None:
    from app.domain.series_preparation import run

    await run(UUID(search_id))


@tasks.task(
    name="acquisition.pack-dispatch",
    queue="acquisition",
    retry=DependencyRetryStrategy(max_attempts=3, wait=10),
)
async def dispatch_automatic_pack(operation_id: str) -> None:
    from app.domain.automatic_packs import run

    await run(UUID(operation_id))


@tasks.task(
    name="organization.reuse",
    queue="imports",
    retry=DependencyRetryStrategy(max_attempts=3, wait=10),
)
async def reuse_completed_transfer(continuation_id: str) -> None:
    from app.importing.reuse import run

    await run(UUID(continuation_id))


@tasks.periodic(cron="*/10 * * * *")
@tasks.task(name="releases.schedule", queue="metadata", retry=3)
async def schedule_releases(timestamp: int) -> None:
    from app.domain.libro_library import schedule as schedule_enrichment
    from app.domain.release_monitor import schedule as schedule_monitors

    await schedule_monitors()
    await schedule_enrichment()


@tasks.periodic(cron="*/10 * * * *")
@tasks.task(name="discovery.schedule", queue="metadata", retry=3)
async def schedule_discovery(timestamp: int) -> None:
    from app.domain.discovery_catalog import schedule

    await schedule()


@tasks.task(name="discovery.refresh", queue="metadata", retry=3)
async def refresh_discovery(user_id: str, collection_id: str, generation: int) -> None:
    from app.domain.discovery_catalog import refresh

    await refresh(UUID(user_id), collection_id, generation)


@tasks.periodic(cron="*/15 * * * *")
@tasks.task(name="series.gap-schedule", queue="metadata", retry=3)
async def schedule_series_gaps(timestamp: int) -> None:
    from app.domain.series_gap_watch import schedule

    await schedule()


@tasks.task(name="series.library_scan", queue="metadata", retry=3)
async def library_scan(user_id: str) -> None:
    from app.domain.series_gap_watch import scan_user

    await scan_user(UUID(user_id))


@tasks.task(name="acquisition.quick-add", queue="sources", retry=3)
async def quick_add_book(operation_id: str) -> None:
    from app.domain.quick_add import run

    await run(UUID(operation_id))


@tasks.periodic(cron="* * * * *")
@tasks.task(name="sources.mam.account", queue="system", retry=3)
async def schedule_mam_account(timestamp: int) -> None:
    if get_settings().recovery_mode:
        return
    from app.domain.account_automation import run

    await run()


@tasks.periodic(cron="* * * * *")
@tasks.task(name="notifications.dispatch", queue="notifications", lock="notifications.dispatch")
async def dispatch_notifications(timestamp: int = 0) -> None:
    from app.notifications.delivery import tick

    await tick()


@tasks.task(name="acquisition.recover-download", queue="downloads", retry=3)
async def recover_failed_download(recovery_id: str) -> None:
    from app.domain.download_recovery import run

    await run(UUID(recovery_id))


@tasks.task(name="acquisition.reject-download", queue="downloads", retry=3)
async def reject_failed_import(attempt_id: str) -> None:
    from app.domain.download_recovery import reject_inspected

    await reject_inspected(UUID(attempt_id))


@tasks.periodic(cron="*/5 * * * *")
@tasks.task(name="connections.health", queue="system", lock="connections.health")
async def check_connection_health(timestamp: int = 0) -> None:
    from app.domain.connection_health import run

    await run()
