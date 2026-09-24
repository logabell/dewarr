"""Owner-scoped release receipts, independent of a particular search result page."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import String, cast, or_, select

from app.db.models import (
    AcquisitionIntent,
    AcquisitionSelection,
    DownloadAttempt,
    DownloadFulfillment,
    DownloadMembership,
    Operation,
    SourceResult,
)
from app.domain.release_blocklist import release_keys
from app.domain.work_graph import family_ids


class ReleaseDownloadStatus(BaseModel):
    state: Literal[
        "preparing",
        "queued",
        "downloading",
        "downloaded",
        "imported",
        "failed",
        "cancelled",
        "selected",
        "needs-review",
    ]
    message: str
    request_id: UUID
    operation_id: UUID | None = None
    attempt_id: UUID | None = None
    progress: float | None = None
    reasons: list[str] = []
    prevent_download: bool = False


def identity(release):
    return next(key for key in release_keys(release)[1] if key.startswith("release:"))


def selection_feedback(operation):
    """Keep the specific reasons even for receipts written by older releases."""
    pinned = operation.payload.get("command", {}).get("result_id")
    reasons = list(
        dict.fromkeys(
            reason
            for decision in operation.payload.get("decisions", [])
            if not pinned or decision["result_id"] == pinned
            for reason in decision.get("reasons", [])
        )
    )
    message = operation.message
    if pinned and operation.status in {"held", "failed"} and reasons:
        message = "This release could not be downloaded. " + "; ".join(reasons)
    return message, reasons


async def for_releases(db, owner_id, work_id, releases):
    wanted = {identity(release) for release in releases}
    if not wanted:
        return {}
    statuses = {}
    receipts = (
        await db.execute(
            select(Operation, SourceResult)
            .join(
                SourceResult,
                cast(SourceResult.id, String) == Operation.payload["command"]["result_id"].astext,
            )
            .join(
                AcquisitionIntent,
                cast(AcquisitionIntent.id, String)
                == Operation.payload["command"]["intent_id"].astext,
            )
            .where(
                Operation.owner_id == owner_id,
                Operation.kind == "acquisition.auto-select",
                SourceResult.owner_id == owner_id,
                AcquisitionIntent.owner_id == owner_id,
                AcquisitionIntent.work_id.in_(family_ids(work_id)),
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
        )
    ).all()
    for operation, result in receipts:
        key = identity(result.release_snapshot)
        if key not in wanted or key in statuses:
            continue
        message, reasons = selection_feedback(operation)
        state = {
            "queued": "preparing",
            "running": "preparing",
            "held": "failed",
            "failed": "failed",
            "cancelled": "cancelled",
            "completed": "selected",
        }.get(operation.status, "needs-review")
        statuses[key] = ReleaseDownloadStatus(
            state=state,
            message=message,
            reasons=reasons,
            request_id=operation.payload["command"]["intent_id"],
            operation_id=operation.id,
            prevent_download=state in {"preparing", "selected"},
        )
    # Transfer membership also covers downloads started through Quick add or a reviewed selection.
    transfers = (
        await db.execute(
            select(AcquisitionSelection, DownloadAttempt, DownloadFulfillment.import_entry_id)
            .join(AcquisitionIntent, AcquisitionIntent.id == AcquisitionSelection.intent_id)
            .outerjoin(
                DownloadMembership, DownloadMembership.selection_id == AcquisitionSelection.id
            )
            .join(
                DownloadAttempt,
                or_(
                    DownloadAttempt.id == DownloadMembership.attempt_id,
                    DownloadAttempt.selection_id == AcquisitionSelection.id,
                ),
            )
            .outerjoin(
                DownloadFulfillment,
                (DownloadFulfillment.attempt_id == DownloadAttempt.id)
                & (DownloadFulfillment.target_id == AcquisitionSelection.target_id),
            )
            .where(
                AcquisitionSelection.owner_id == owner_id,
                AcquisitionIntent.owner_id == owner_id,
                DownloadAttempt.owner_id == owner_id,
                AcquisitionIntent.work_id.in_(family_ids(work_id)),
            )
            .order_by(DownloadAttempt.created_at.desc(), DownloadAttempt.id.desc())
        )
    ).all()
    seen = set()
    for selection, attempt, imported_entry in transfers:
        release = selection.frozen.get("release")
        if not release:
            continue
        key = identity(release)
        if key not in wanted or key in seen:
            continue
        seen.add(key)
        # An old cancelled transfer must not mask a new preparation or its failure.
        if attempt.state == "cancelled" and key in statuses:
            continue
        state = {
            "queued": "queued",
            "preflight": "queued",
            "submitting": "queued",
            "downloading": "downloading",
            "complete": "downloaded",
            "held": "needs-review",
            "uncertain": "needs-review",
            "cancelled": "cancelled",
        }[attempt.state]
        message = attempt.message
        if imported_entry:
            state, message = (
                "imported",
                "This release was downloaded and imported into your library.",
            )
        elif state == "downloaded":
            message = "Download complete. Waiting for library import confirmation."
        progress = (attempt.observation or {}).get("progress")
        statuses[key] = ReleaseDownloadStatus(
            state=state,
            message=message,
            request_id=selection.intent_id,
            attempt_id=attempt.id,
            operation_id=selection.frozen.get("automatic_selection", {}).get("operation_id"),
            progress=progress
            if isinstance(progress, int | float) and not isinstance(progress, bool)
            else None,
            prevent_download=state != "cancelled",
        )
    return statuses
