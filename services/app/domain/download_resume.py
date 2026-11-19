"""Continue an interrupted transfer once the release that stopped it is unblocked.

A release is blocklisted when a transfer is judged failed. The torrent itself, its
hash claims and its files stay on the downloader, so a mistaken or outdated block
should not force a new download. Resuming reverses only the recovery bookkeeping
(cancelled selection, released reservation, recovery row) and lets the existing
attempt observe the existing transfer again. It never adds a torrent.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    AuditEvent,
    DownloadAttempt,
    DownloadRecovery,
    Operation,
    ReportedDownloadAsset,
    User,
)
from app.domain import download_attempts, release_blocklist
from app.domain.acquisition import evaluate
from app.domain.work_graph import canonical_work
from app.jobs.queue import enqueue


async def resumable_attempt_ids(db, block):
    """Held attempts whose only obstacle is this block; the cheap pre-lock screen."""
    rows = await db.execute(
        select(DownloadRecovery, AcquisitionSelection)
        .join(AcquisitionSelection, AcquisitionSelection.id == DownloadRecovery.selection_id)
        .join(DownloadAttempt, DownloadAttempt.id == DownloadRecovery.attempt_id)
        .where(
            DownloadRecovery.state == "held",
            DownloadAttempt.state == "held",
            DownloadAttempt.external_may_exist.is_(True),
            AcquisitionSelection.state == "cancelled",
        )
        .order_by(DownloadRecovery.created_at, DownloadRecovery.id)
    )
    found = []
    for recovery, selection in rows:
        frozen = selection.frozen
        if frozen["requirements"]["medium"] != block.medium:
            continue
        keys = set(release_blocklist.release_keys(frozen["release"], frozen["descriptor"])[1])
        if not keys.intersection(block.identities):
            continue
        work = await canonical_work(db, UUID(frozen["origin_work_id"]))
        if work.id == block.work_id and recovery.attempt_id not in found:
            found.append(recovery.attempt_id)
    return found


async def _refusal(db, attempt, recoveries):
    """Why this attempt cannot be resumed, or None. Caller holds the attempt locks."""
    if get_settings().recovery_mode:
        return "Downloads are paused for recovery"
    if attempt.state != "held" or not attempt.external_may_exist:
        return "The download is no longer held"
    if not recoveries:
        return "The download has no saved recovery to undo"
    if any(row.state != "held" for row in recoveries):
        return "A replacement search or download already followed this failure"
    if any((row.evidence or {}).get("reported") for row in recoveries):
        return "This release was reported as a problem by a person; it cannot be resumed"
    if await db.scalar(
        select(ReportedDownloadAsset.id)
        .where(ReportedDownloadAsset.recovery_id.in_([row.id for row in recoveries]))
        .limit(1)
    ):
        return "This release was reported as a problem by a person; it cannot be resumed"
    return None


async def _queue_check(db, attempt):
    """Same hand-off as an explicit recheck: one job, never two."""
    operation = await db.get(Operation, attempt.operation_id)
    status = await db.scalar(
        text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
        {"id": operation.job_id},
    )
    if status not in {"todo", "doing"}:
        operation.job_id = await enqueue(db, "acquisition.download", attempt_id=str(attempt.id))
    attempt.next_check_at = datetime.now(UTC) + timedelta(seconds=60)


async def _resume_attempt(db, user, attempt_id, block):
    attempt, _ = await download_attempts.locked(db, attempt_id)
    if not attempt:
        return "The download no longer exists"
    recoveries = list(
        await db.scalars(
            select(DownloadRecovery)
            .where(DownloadRecovery.attempt_id == attempt.id)
            .execution_options(populate_existing=True)
        )
    )
    if reason := await _refusal(db, attempt, recoveries):
        return reason
    selections = [
        await db.get(AcquisitionSelection, row.selection_id, populate_existing=True)
        for row in recoveries
    ]
    if any(item.state != "cancelled" for item in selections):
        return "The request moved on after this failure"
    block.active = False
    await db.flush()
    for item in selections:
        frozen = item.frozen
        if await release_blocklist.blocked(
            db,
            (await canonical_work(db, UUID(frozen["origin_work_id"]))).id,
            frozen["requirements"]["medium"],
            frozen["release"],
            frozen["descriptor"],
        ):
            return "Another blocklist entry still covers this release"
    try:
        async with db.begin_nested():
            for row, item in zip(recoveries, selections, strict=True):
                item.state, item.message = "committed", "Resumed after the release was unblocked"
                (await db.get(AcquisitionReservation, item.reservation_id)).state = "committed"
                await db.delete(row)
            await db.flush()
            for item in selections:
                owner = await db.get(User, item.owner_id)
                await evaluate(db, owner, await db.get(AcquisitionIntent, item.intent_id))
                await db.flush()
                target = await db.get(AcquisitionTarget, item.target_id, populate_existing=True)
                if target.state != "wanted" or target.reservation_id != item.reservation_id:
                    raise HTTPException(
                        409, "The request is paused, withdrawn or already satisfied"
                    )
    except HTTPException as error:
        return str(error.detail)
    attempt.recovery_observation = None
    attempt.message = "Resumed after unblocking; checking the existing transfer"
    (await db.get(Operation, attempt.operation_id)).message = attempt.message
    await _queue_check(db, attempt)
    db.add(
        AuditEvent(
            actor_id=user.id,
            action="acquisition.download.resumed",
            entity_id=attempt.id,
            detail={
                "block_id": str(block.id),
                "selection_ids": [str(item.id) for item in selections],
                "reasons": sorted({row.reason for row in recoveries}),
            },
        )
    )
    return None


async def unblock(db, user, block_id, *, resume=True):
    """Deactivate a block and, when possible, continue the transfer it stopped.

    Returns (block, results) with one (attempt_id, message-or-None) per candidate;
    None means resumed. The block is removed even when nothing can be resumed.
    """
    from app.db.models import ReleaseBlock
    from app.domain.work_graph import acquisition_lock

    block = await db.get(ReleaseBlock, block_id)
    if not block:
        raise HTTPException(404, "Blocklist entry not found")
    results = []
    if resume and block.active:
        for attempt_id in await resumable_attempt_ids(db, block):
            results.append((attempt_id, await _resume_attempt(db, user, attempt_id, block)))
    if block.active:
        # Plain removal, or no candidate: same lock the plain endpoint always took.
        await acquisition_lock(db, block.work_id)
        block.active = False
    db.add(AuditEvent(actor_id=user.id, action="acquisition.blocklist.removed", entity_id=block.id))
    return block, results


__all__ = ["resumable_attempt_ids", "unblock"]
