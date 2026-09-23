from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.api.dependencies import Admin, Database, Member
from app.db.models import (
    AcquisitionSelection,
    AuditEvent,
    DownloadRecovery,
    DownloadRecoverySettings,
    FrozenImportPlan,
    ImportEntry,
    ImportRun,
    ReleaseBlock,
    ReportedDownloadAsset,
    User,
    Version,
    Work,
)
from app.domain import download_recovery as recovery
from app.domain.download_attempts import locked
from app.domain.operations import transaction_lock
from app.domain.work_graph import family_ids
from app.jobs.queue import enqueue

router = APIRouter(prefix="/acquisition/recovery", tags=["download-recovery"])


class BlockView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    work_id: UUID
    medium: str
    source: str
    title: str
    reason: str
    actor_id: UUID
    automatic: bool
    created_at: datetime
    work_title: str
    actor_name: str


class DownloadRecoveryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    selection_id: UUID
    attempt_id: UUID
    state: str
    reason: str
    message: str
    replacement_id: UUID | None


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection_id: UUID
    asset_id: UUID | None = None
    reason: Literal[
        "wrong-book",
        "wrong-language",
        "bad-audio",
        "missing-chapters",
        "wrong-narrator",
        "drm",
        "incomplete",
    ]
    require_approval: bool = False


class RecoveryApprovalView(DownloadRecoveryView):
    work_title: str


@router.get("/approvals", response_model=list[RecoveryApprovalView])
async def pending_approvals(user: Admin, db: Database):
    rows = await db.execute(
        select(DownloadRecovery, AcquisitionSelection)
        .join(AcquisitionSelection, AcquisitionSelection.id == DownloadRecovery.selection_id)
        .where(DownloadRecovery.state == "approval")
        .order_by(DownloadRecovery.created_at, DownloadRecovery.id)
        .limit(100)
    )
    return [
        RecoveryApprovalView(
            **DownloadRecoveryView.model_validate(row).model_dump(),
            work_title=selection.frozen["work_title"],
        )
        for row, selection in rows
    ]


@router.get("/settings", response_model=recovery.RecoveryConfiguration)
async def settings(user: Admin, db: Database):
    return await recovery.configuration(db)


@router.put("/settings", response_model=recovery.RecoveryConfiguration)
async def save_settings(body: recovery.RecoveryConfiguration, user: Admin, db: Database):
    await transaction_lock(db, "download-recovery-settings")
    row = await db.get(DownloadRecoverySettings, 1)
    if row is None:
        row = DownloadRecoverySettings(id=1)
        db.add(row)
    row.configuration = body.model_dump(mode="json")
    db.add(AuditEvent(actor_id=user.id, action="acquisition.recovery.settings"))
    await db.commit()
    return body


@router.get("/blocklist", response_model=list[BlockView])
async def blocklist(
    user: Admin, db: Database, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    rows = await db.execute(
        select(ReleaseBlock, Work.title, User.display_name)
        .join(Work, Work.id == ReleaseBlock.work_id)
        .join(User, User.id == ReleaseBlock.actor_id)
        .where(ReleaseBlock.active.is_(True))
        .order_by(ReleaseBlock.created_at.desc(), ReleaseBlock.id)
        .offset(offset)
        .limit(limit)
    )
    return [
        {
            **{
                name: getattr(block, name)
                for name in BlockView.model_fields
                if name not in {"work_title", "actor_name"}
            },
            "work_title": title,
            "actor_name": actor,
        }
        for block, title, actor in rows
    ]


@router.delete("/blocklist/{block_id}", status_code=204)
async def remove_block(block_id: UUID, user: Admin, db: Database):
    block = await db.get(ReleaseBlock, block_id)
    if not block:
        raise HTTPException(404, "Blocklist entry not found")
    from app.domain.work_graph import acquisition_lock

    await acquisition_lock(db, block.work_id)
    block.active = False
    db.add(AuditEvent(actor_id=user.id, action="acquisition.blocklist.removed", entity_id=block.id))
    await db.commit()


@router.post("/reports", response_model=list[DownloadRecoveryView], status_code=202)
async def report(body: ReportInput, user: Member, db: Database):
    from app.domain.download_memberships import attempt_for
    from app.domain.download_reviews import has_imports

    selection = await db.get(AcquisitionSelection, body.selection_id)
    if not selection or selection.owner_id != user.id:
        raise HTTPException(404, "Download selection not found")
    attempt = await attempt_for(db, selection.id)
    if not attempt:
        raise HTTPException(409, "This release has not been downloaded")
    attempt, _ = await locked(db, attempt.id)
    existing = await db.scalar(
        select(DownloadRecovery).where(DownloadRecovery.selection_id == selection.id)
    )
    if existing:
        return [DownloadRecoveryView.model_validate(existing)]
    if attempt.state != "complete":
        raise HTTPException(409, "Reconcile the existing transfer before reporting its content")
    imports = (
        list(
            await db.scalars(
                select(ImportEntry)
                .join(ImportRun)
                .join(FrozenImportPlan)
                .where(FrozenImportPlan.inspection_id == attempt.inspection_id)
            )
        )
        if attempt.inspection_id
        else []
    )
    if any(
        entry.state != "confirmed" and (entry.reserved or entry.published_at) for entry in imports
    ):
        raise HTTPException(
            409, "Finish or cancel the active imports before reporting this release"
        )
    if body.asset_id:
        # A fulfillment from an unrelated existing copy is not provenance. Only a
        # published/confirmed import from this attempt can identify its release.
        provenance = await db.scalar(
            select(ImportEntry.id)
            .join(ImportRun)
            .join(FrozenImportPlan)
            .join(Version, Version.id == ImportEntry.version_id)
            .where(
                FrozenImportPlan.inspection_id == attempt.inspection_id,
                ImportEntry.asset_id == body.asset_id,
                ImportEntry.state == "confirmed",
                ImportEntry.destination_id == selection.destination_id,
                Version.work_id.in_(family_ids(UUID(selection.frozen["origin_work_id"]))),
            )
        )
        if not provenance:
            raise HTTPException(409, "This library copy has no confirmed import from that release")
        from app.domain.download_reviews import requester_authority

        await requester_authority(db, selection)
    elif attempt.inspection_id and await has_imports(db, attempt.inspection_id):
        raise HTTPException(409, "Choose the imported library copy before reporting a problem")
    rows = await recovery.failed(
        db,
        attempt,
        "Reported problem: " + body.reason,
        actor_id=user.id,
        approval=body.require_approval or (await recovery.configuration(db)).approve_reports,
    )
    # Rejecting a shared release gives every member its own fallback. Exclude only
    # confirmed copies from this transfer, scoped to each member and destination.
    for row in rows:
        member = await db.get(AcquisitionSelection, row.selection_id)
        asset_ids = await db.scalars(
            select(ImportEntry.asset_id)
            .join(Version, Version.id == ImportEntry.version_id)
            .where(
                ImportEntry.id.in_([entry.id for entry in imports]),
                ImportEntry.state == "confirmed",
                ImportEntry.asset_id.is_not(None),
                ImportEntry.destination_id == member.destination_id,
                Version.work_id.in_(family_ids(UUID(member.frozen["origin_work_id"]))),
                Version.medium == member.frozen["requirements"]["medium"],
            )
        )
        for asset_id in asset_ids:
            previous = await db.scalar(
                select(ReportedDownloadAsset).where(
                    ReportedDownloadAsset.owner_id == member.owner_id,
                    ReportedDownloadAsset.asset_id == asset_id,
                )
            )
            if not previous:
                db.add(
                    ReportedDownloadAsset(
                        owner_id=member.owner_id, asset_id=asset_id, recovery_id=row.id
                    )
                )
    result = [
        DownloadRecoveryView.model_validate(row)
        for row in rows
        if (await db.get(AcquisitionSelection, row.selection_id)).owner_id == user.id
    ]
    await db.commit()
    return result


@router.post("/{recovery_id}/approve", response_model=DownloadRecoveryView, status_code=202)
async def approve(recovery_id: UUID, user: Admin, db: Database):
    await transaction_lock(db, f"download-recovery:{recovery_id}")
    row = await db.get(DownloadRecovery, recovery_id)
    if not row:
        raise HTTPException(404, "Recovery not found")
    if row.state == "approval":
        row.state, row.message = "queued", "Replacement approved; finding the next eligible release"
        row.job_id = await enqueue(db, recovery.KIND, recovery_id=str(row.id))
        db.add(
            AuditEvent(actor_id=user.id, action="acquisition.recovery.approved", entity_id=row.id)
        )
    result = DownloadRecoveryView.model_validate(row)
    await db.commit()
    return result
