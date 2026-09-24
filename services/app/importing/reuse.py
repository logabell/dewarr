"""Independent import continuation for an authorized join to a completed transfer."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.qbittorrent import verify_association
from app.config import get_settings
from app.db.models import (
    AcquisitionSelection,
    AutomaticImportContinuation,
    DownloadAttempt,
    DownloadInspection,
    Operation,
)
from app.db.session import session_factory
from app.domain import download_attempts, download_reviews
from app.domain.acquisition import RequestSpec, assess
from app.domain.operations import transaction_lock
from app.importing import automatic
from app.jobs.queue import enqueue
from app.jobs.retry import DependencyRetry
from app.security import decrypt_secrets

KIND = "organization.reuse"


async def schedule(db, attempt, join, selections):
    if await db.scalar(
        select(AutomaticImportContinuation.id).where(
            AutomaticImportContinuation.join_operation_id == join.id
        )
    ):
        return
    approval = selections[0].frozen["automatic_selection"]["dispatch_approval"]
    if any(
        item.frozen["automatic_selection"]["dispatch_approval"] != approval for item in selections
    ):
        raise HTTPException(409, "Joined books need the same current automatic import approval")
    operation = Operation(
        owner_id=UUID(approval["approved_by"]),
        kind=KIND,
        idempotency_key=f"reuse-import:{join.id}",
        payload={"attempt_id": str(attempt.id), "join_operation_id": str(join.id)},
    )
    db.add(operation)
    await db.flush()
    row = AutomaticImportContinuation(
        attempt_id=attempt.id,
        join_operation_id=join.id,
        policy_id=UUID(approval["policy_id"]),
        policy_generation=approval["policy_generation"],
        operation_id=operation.id,
        inspection_id=attempt.inspection_id,
        evidence={"authorized_selection_ids": [str(item.id) for item in selections]},
    )
    db.add(row)
    await db.flush()
    # A completed reviewed transfer may not yet have an automatic inspection.
    if not attempt.inspection_id:
        await automatic.schedule(db, attempt, selections[0])
    operation.job_id = await enqueue(db, KIND, continuation_id=str(row.id))


async def probe(attempt, selection, downloader):
    credentials = decrypt_secrets(downloader.encrypted_secrets)
    async with asyncio.timeout(45):
        factory = {
            "transmission": download_attempts.TransmissionClient,
            "deluge": download_attempts.DelugeClient,
        }.get(downloader.kind, download_attempts.QbitClient)
        async with factory(
            downloader.base_url, credentials["username"], credentials["password"]
        ) as client:
            await client.capabilities()
            states = await download_attempts.find(
                client, selection, download_attempts.attempt_tag(attempt)
            )
    from app.adapters.torrent_rpc import verify_untagged

    verify = verify_untagged if downloader.kind == "deluge" else verify_association
    observed = verify(
        states,
        tag=download_attempts.attempt_tag(attempt),
        hashes=download_attempts.hashes(selection),
        save_path=selection.frozen["downloader"]["save_path"],
        category=selection.frozen["downloader"]["category"],
    )
    expected = {f["path"]: f["size_bytes"] for f in selection.frozen["descriptor"]["files"]}
    if (
        not observed
        or not observed.completed
        or {f.relative_path: f.size_bytes for f in observed.files} != expected
        or observed.total_bytes != selection.frozen["descriptor"]["torrent_bytes"]
    ):
        raise HTTPException(
            409, "The saved torrent no longer confirms all completed files; review reuse"
        )
    return {
        "external_id": observed.external_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "complete": True,
    }


async def hold(identifier, message):
    async with session_factory()() as db, db.begin():
        row = await db.get(AutomaticImportContinuation, identifier)
        if not row:
            return
        await download_attempts.locked(db, row.attempt_id)
        await transaction_lock(db, f"automatic-reuse:{identifier}")
        row = await db.get(AutomaticImportContinuation, identifier, populate_existing=True)
        if row and row.state in {"queued", "inspecting"}:
            row.state, row.message = "held", message
            op = await db.get(Operation, row.operation_id)
            op.status, op.message = "failed", message
            (await db.get(DownloadAttempt, row.attempt_id)).message = message


async def requesting_member(db, row):
    """Select a surviving joined request; an earlier sibling may already be owned."""
    satisfied, errors = 0, []
    for value in row.evidence["authorized_selection_ids"]:
        selection = await db.get(AcquisitionSelection, UUID(value), populate_existing=True)
        try:
            owner, intent, _ = await download_reviews.requester_authority(db, selection)
            outcomes = await assess(
                db, owner, intent.work_id, RequestSpec.model_validate(intent.specification)
            )
            if any(
                item["slot"] == selection.frozen["slot"] and item["state"] == "satisfied"
                for item in outcomes
            ):
                satisfied += 1
                await enqueue(
                    db, "acquisition.fulfillment", work_id=selection.frozen["origin_work_id"]
                )
                continue
            downloader, _ = await download_attempts.selection_authority(db, selection, wanted=False)
            return selection, downloader
        except HTTPException as error:
            errors.append(str(error.detail))
    if satisfied == len(row.evidence["authorized_selection_ids"]):
        row.state, row.message = (
            "complete",
            "Joined books are already available; no files republished",
        )
        operation = await db.get(Operation, row.operation_id)
        operation.status, operation.message = "completed", row.message
        return None
    raise HTTPException(409, errors[0] if errors else "Joined requests need review before import")


async def run(identifier):
    if get_settings().recovery_mode:
        raise DependencyRetry(60)
    try:
        async with session_factory()() as db, db.begin():
            row = await db.get(AutomaticImportContinuation, identifier)
            if not row or row.state not in {"queued", "inspecting"}:
                return
            await download_attempts.locked(db, row.attempt_id)
            started = datetime.fromisoformat(
                row.evidence.get("verification_started_at", row.created_at.isoformat())
            )
            if started < datetime.now(UTC) - timedelta(minutes=15):
                raise HTTPException(409, "Reuse verification expired; recheck this saved transfer")
            await automatic.check_policy(db, row)
            requesting = await requesting_member(db, row)
            if requesting is None:
                return
            selection, downloader = requesting
            attempt = await db.get(DownloadAttempt, row.attempt_id)
            inspection = (
                await db.get(DownloadInspection, attempt.inspection_id)
                if attempt.inspection_id
                else None
            )
            if not inspection or inspection.state not in {"ready", "failed"}:
                raise DependencyRetry(5)
            if inspection.state != "ready":
                raise HTTPException(409, "File inspection needs review before reuse")
            observed_inspection = inspection.id
        observed = await probe(attempt, selection, downloader)
        async with session_factory()() as db, db.begin():
            await download_attempts.locked(db, attempt.id)
            await transaction_lock(db, f"automatic-reuse:{identifier}")
            row = await db.get(AutomaticImportContinuation, identifier, populate_existing=True)
            if row.state not in {"queued", "inspecting"}:
                return
            async with db.begin_nested():
                _, approver, destination, current = await automatic.check_policy(db, row)
                attempt = await db.get(DownloadAttempt, row.attempt_id)
                if attempt.state != "complete" or attempt.inspection_id != observed_inspection:
                    raise HTTPException(409, "Saved transfer inspection changed; review reuse")
                requesting = await requesting_member(db, row)
                if requesting is None:
                    return
                selection, _ = requesting
                row.inspection_id = observed_inspection
                row.evidence = {**row.evidence, "reuse_observation": observed}
                await automatic.plan_ready(
                    db,
                    row,
                    selection,
                    await db.get(DownloadInspection, observed_inspection),
                    approver,
                    destination,
                    current,
                )
                operation = await db.get(Operation, row.operation_id)
                operation.status = "failed" if row.state == "held" else "completed"
                operation.message = row.message
                attempt.message = row.message
                for value in row.evidence["authorized_selection_ids"]:
                    item = await db.get(AcquisitionSelection, UUID(value))
                    await enqueue(
                        db, "acquisition.fulfillment", work_id=item.frozen["origin_work_id"]
                    )
    except DependencyRetry:
        raise
    except (HTTPException, AdapterError, TimeoutError) as error:
        if (
            isinstance(error, TimeoutError)
            or isinstance(error, AdapterError)
            and error.kind in {FailureKind.RATE_LIMIT, FailureKind.UNAVAILABLE, FailureKind.TIMEOUT}
        ):
            raise DependencyRetry(60) from error
        await hold(
            identifier, str(error.detail) if isinstance(error, HTTPException) else str(error)
        )


async def recheck(db, attempt):
    rows = list(
        await db.scalars(
            select(AutomaticImportContinuation).where(
                AutomaticImportContinuation.attempt_id == attempt.id,
                AutomaticImportContinuation.state.in_(["held", "queued", "inspecting"]),
            )
        )
    )
    for row in rows:
        await transaction_lock(db, f"automatic-reuse:{row.id}")
        await db.refresh(row)
        if row.state not in {"held", "queued", "inspecting"}:
            continue
        operation = await db.get(Operation, row.operation_id, populate_existing=True)
        status = await db.scalar(
            text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
            {"id": operation.job_id},
        )
        if row.state != "held" and status in {"todo", "doing"}:
            continue
        # Rechecking the saved authority never creates a new join or physical attempt.
        await automatic.check_policy(db, row)
        row.state, row.message = "queued", "Rechecking the saved transfer for joined books"
        row.evidence = {**row.evidence, "verification_started_at": datetime.now(UTC).isoformat()}
        operation.status, operation.message = "queued", row.message
        operation.job_id = await enqueue(db, KIND, continuation_id=str(row.id))


async def recover(db, identifier):
    """Make stopped continuations actionable without silently renewing retry budgets."""
    await transaction_lock(db, f"automatic-reuse:{identifier}")
    row = await db.get(AutomaticImportContinuation, identifier, populate_existing=True)
    if not row or row.state not in {"queued", "inspecting"}:
        return
    operation = await db.get(Operation, row.operation_id, populate_existing=True)
    status = await db.scalar(
        text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
        {"id": operation.job_id},
    )
    if status in {"todo", "doing"}:
        return
    if status in {"failed", "aborted"}:
        row.state, row.message = (
            "held",
            "Saved-transfer verification stopped; recheck this transfer to retry joined books",
        )
        operation.status, operation.message = "failed", row.message
        return
    operation.job_id = await enqueue(db, KIND, continuation_id=str(row.id))
