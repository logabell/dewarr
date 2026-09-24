"""Author and series sources share subscriptions, policies, reasons and release monitors."""

import logging
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import select

from app.db.models import ListAcquisitionBook, ListObservation, ListSubscription, Operation
from app.domain.release_dates import assign_release, release_facts, search_allowed
from app.domain.work_graph import canonical_work
from app.security import decrypt_secrets

logger = logging.getLogger(__name__)


async def source(db, list_id):
    row = await db.scalar(select(ListSubscription).where(ListSubscription.list_id == list_id))
    if row and row.source_kind:
        config = decrypt_secrets(row.encrypted_config)
        if config.get("source_kind") in {"author", "series"}:
            return row, config
    return None


async def record_discoveries(db, row, owner, config, previous):
    """NOR-30 owns delivery; missing integration is visible in logs, never a fake delivery."""
    from app.adapters.catalog_types import cover_url

    observations = [
        observation
        for observation in await db.scalars(
            select(ListObservation).where(ListObservation.subscription_id == row.id)
        )
        if observation.external_id not in previous
        and observation.present
        and not observation.excluded
        and not observation.snapshot.get("filter_reason")
    ]
    if not observations:
        return
    try:
        from app.notifications.events import record_event
    except ModuleNotFoundError as error:
        if error.name not in {"app.notifications", "app.notifications.events"}:
            raise
        logger.warning(
            "Follow discovery delivery unavailable: NOR-30 notifications is not installed"
        )
        return
    for observation in observations:
        await record_event(
            db,
            key=f"follow:{row.id}:work:{observation.work_id}",
            event_type=f"discovery.{config['source_kind']}",
            owner_id=owner.id,
            subject_id=observation.work_id,
            title="New book from a follow",
            message=observation.snapshot["title"],
            path=f"/books/{observation.work_id}",
            cover_url=cover_url(observation.snapshot.get("cover_url")),
        )


async def publish_metadata(db, row, owner):
    from app.api.list_subscriptions import remove_unneeded
    from app.db.models import ListAcquisitionPolicy, MonitoredRelease
    from app.domain.release_monitor import record_release_day
    from app.domain.work_graph import family_ids

    for observation in await db.scalars(
        select(ListObservation).where(ListObservation.subscription_id == row.id)
    ):
        work = await canonical_work(db, observation.work_id)
        record = observation.snapshot
        if record.get("filter_reason"):
            await remove_unneeded(db, owner, row, observation.work_id)
        if record.get("coming_soon") and not record.get("release_date"):
            work.metadata_fields = assign_release(
                work.metadata_fields, None, "unknown", coming_soon=True, source="hardcover"
            )
        if record.get("release_date"):
            from datetime import date

            day = date.fromisoformat(record["release_date"])
            work.metadata_fields = assign_release(
                work.metadata_fields, day, "work", source="hardcover"
            )
            stored, _, _ = release_facts(work.metadata_fields)
            # The subscription holds the list lock, matching list-worker lock
            # order. Refresh its deadline before touching a shared monitor row.
            for book in await db.scalars(
                select(ListAcquisitionBook)
                .join(ListAcquisitionPolicy)
                .where(
                    ListAcquisitionPolicy.list_id == row.list_id,
                    ListAcquisitionBook.generation == ListAcquisitionPolicy.generation,
                    ListAcquisitionBook.work_id.in_(family_ids(work.id)),
                    ListAcquisitionBook.progress["waiting_for_release"].as_boolean().is_(True),
                )
            ):
                book.next_check_at = datetime.combine(stored, time.min, tzinfo=UTC)
            await db.flush()
            monitor = await db.scalar(
                select(MonitoredRelease).where(
                    MonitoredRelease.owner_id == owner.id, MonitoredRelease.work_id == work.id
                )
            )
            if monitor:
                await record_release_day(db, monitor, work, day, "work", source="hardcover")
                operation = (
                    await db.get(Operation, monitor.operation_id) if monitor.operation_id else None
                )
                if (
                    operation
                    and operation.kind == "lists.release-wait"
                    and monitor.state == "waiting"
                ):
                    # Unlike a discovery suggestion, the verified catalog can
                    # move its own still-waiting release both earlier and later.
                    stored, basis, _ = release_facts(work.metadata_fields)
                    monitor.release_date, monitor.basis = stored, basis
                    monitor.next_check_at = datetime.combine(stored, time.min, tzinfo=UTC)
                    operation.payload = {
                        **operation.payload,
                        "waiting_for_release": stored.isoformat(),
                    }


async def wait_for_release(db, user, policy, book, now):
    """Use the release monitor without granting a second, manual acquisition reason."""
    from app.db.models import MonitoredRelease
    from app.domain.operations import transaction_lock
    from app.domain.release_monitor import sync_monitor
    from app.domain.work_graph import acquisition_lock

    if not await source(db, policy.list_id):
        return False
    work = await canonical_work(db, book.work_id)
    day, _, coming = release_facts(work.metadata_fields)
    if search_allowed(day, now.date(), coming_soon=coming):
        if book.progress.get("waiting_for_release"):
            book.progress = {
                key: value for key, value in book.progress.items() if key != "waiting_for_release"
            }
        return False
    await acquisition_lock(db, work.id)
    await transaction_lock(db, f"monitored-release:{user.id}:{work.id}")
    key = f"follow-release:{book.id}:{policy.generation}"
    operation = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if not operation:
        operation = Operation(
            owner_id=user.id,
            kind="lists.release-wait",
            idempotency_key=key,
            status="held",
            message="Waiting for release day",
            payload={
                "book_id": str(book.id),
                "policy_id": str(policy.id),
                "generation": policy.generation,
                "waiting_for_release": day.isoformat() if day else None,
            },
        )
        db.add(operation)
        await db.flush()
    monitor = await db.scalar(
        select(MonitoredRelease).where(
            MonitoredRelease.owner_id == user.id, MonitoredRelease.work_id == work.id
        )
    )
    previous = (
        await db.get(Operation, monitor.operation_id) if monitor and monitor.operation_id else None
    )
    # A separately followed book retains its own request if this author/series is
    # later unfollowed. The list's own due date still wakes its acquisition worker.
    if not previous or previous.kind == "lists.release-wait":
        await sync_monitor(db, user, work, operation, policy.configuration["specification"])
    book.state = "wanted"
    book.progress = {**book.progress, "waiting_for_release": True}
    book.message = f"Waiting until {day.isoformat()}" if day else "Waiting for a release date"
    book.next_check_at = (
        datetime.combine(day, time.min, tzinfo=UTC) if day else now + timedelta(days=1)
    )
    return True


async def release_ready(db, row, operation, now):
    from app.db.models import ListAcquisitionPolicy, User

    payload = operation.payload
    policy = await db.get(ListAcquisitionPolicy, UUID(payload["policy_id"]))
    book = await db.get(ListAcquisitionBook, UUID(payload["book_id"]))
    if not policy or not book or policy.generation != payload["generation"]:
        row.state, row.next_check_at = "stopped", None
        return
    if not policy.active or book.state in {"baseline", "removed"}:
        row.next_check_at = now + timedelta(days=1)
        return
    user = await db.get(User, row.owner_id)
    work = await canonical_work(db, book.work_id)
    from app.domain.availability import availability_for
    from app.domain.release_monitor import _owned

    if _owned((await availability_for(db, user, [work.id])).get(work.id), row.specification):
        row.state, row.next_check_at = "available", None
        return
    # List workers already hold the release deadline and revalidate all authority.
    # Never acquire their list/policy locks while the monitor row is locked.
    operation.status, operation.message = "completed", "Release day reached; list policy resumes"
    row.state, row.next_check_at = "wanted", now + timedelta(hours=6)
