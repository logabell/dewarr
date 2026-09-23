from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from app.adapters.contracts import AdapterError
from app.api.dependencies import Admin, CurrentUser, Database, Member
from app.api.metadata import adapter_http_error
from app.db.models import (
    AcquisitionIntent,
    AcquisitionSelection,
    AcquisitionTarget,
    AutomaticImport,
    AutomaticImportContinuation,
    DownloadAttempt,
    DownloadFulfillment,
    DownloadInspection,
    DownloadMembership,
    DownloadRecovery,
    ImportEntry,
)
from app.domain import download_attempts as downloads
from app.domain import download_memberships
from app.domain import download_repairs as repairs
from app.domain.acquisition import RequestSpec, assess
from app.domain.acquisition_selection import configuration_current
from app.domain.work_graph import family_ids
from app.importing.starting import MERGE_PENDING_MESSAGE

router = APIRouter(prefix="/acquisition/downloads", tags=["downloads"])


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection_id: UUID
    additional_selection_ids: list[UUID] = Field(default_factory=list, max_length=99)


class FulfillmentView(BaseModel):
    confirmed_at: datetime
    basis: str
    available_now: bool


class DownloadMemberView(BaseModel):
    selection_id: UUID
    intent_id: UUID
    work_title: str
    medium: str
    state: str
    target_state: str
    message: str
    fulfillment: FulfillmentView | None
    join_operation_id: UUID | None = None


class ImportContinuationView(BaseModel):
    id: UUID
    state: str
    message: str
    selection_ids: list[UUID]


class RepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class RepairPreview(BaseModel):
    revision: str
    changes: list[str]


class RepairView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    state: str
    message: str
    changes: list[str]
    created_at: datetime
    applied_at: datetime | None


class RecoveryStepView(BaseModel):
    attempt_id: UUID
    selection_id: UUID
    release_title: str
    state: str
    reason: str


class RecoveryStatusView(BaseModel):
    id: UUID
    state: str
    reason: str
    message: str
    cleanup: str
    can_approve: bool


class AttemptView(BaseModel):
    id: UUID
    created_at: datetime
    selection_id: UUID
    operation_id: UUID
    state: str
    work_title: str
    release_title: str
    source: str | None = None
    message: str
    external_may_exist: bool
    can_recheck: bool
    can_cancel: bool
    progress: float | None
    inspection_id: UUID | None
    fulfillment: FulfillmentView | None
    repair: RepairView | None
    can_repair: bool
    members: list[DownloadMemberView]
    import_continuations: list[ImportContinuationView] = Field(default_factory=list)
    attempt_chain: list[RecoveryStepView] = Field(default_factory=list)
    recoveries: list[RecoveryStatusView] = Field(default_factory=list)
    can_report_problem: bool = False
    imported_asset_id: UUID | None = None


class AttemptPage(BaseModel):
    items: list[AttemptView]
    total: int
    offset: int
    limit: int


async def view(db, user, row, selection):
    message = row.message
    automatic = await db.scalar(select(AutomaticImport).where(AutomaticImport.attempt_id == row.id))
    if automatic and automatic.state == "held":
        message = automatic.message
    if automatic and automatic.import_run_id:
        if await db.scalar(
            select(ImportEntry.id).where(
                ImportEntry.run_id == automatic.import_run_id,
                ImportEntry.state.in_(["held", "cancel-held"]),
            )
        ):
            message = "Import needs administrator attention; the completed download is preserved"
        elif await db.scalar(
            select(ImportEntry.id).where(
                ImportEntry.run_id == automatic.import_run_id,
                ImportEntry.state.in_(["queued", "publishing"]),
                ImportEntry.message == MERGE_PENDING_MESSAGE,
            )
        ):
            message = automatic.message
    inspection = await db.get(DownloadInspection, row.inspection_id) if row.inspection_id else None
    repair = await repairs.latest(db, row.id)
    repairing = bool(repair and repair.state == "pending")
    repairable = (
        user.role == "admin"
        and row.external_may_exist
        and not repairing
        and row.state not in {"complete", "cancelled"}
        and (not row.lease_until or row.lease_until <= datetime.now(UTC))
    )
    needs_review = repairable and not await configuration_current(
        db,
        selection,
        committed=True,
        configuration=await repairs.accepted_configuration(db, selection),
    )
    members = []
    memberships = {
        item.selection_id: item
        for item in await db.scalars(
            select(DownloadMembership).where(DownloadMembership.attempt_id == row.id)
        )
    }
    for item in await download_memberships.for_attempt(db, row.id):
        if item.owner_id != user.id:
            continue
        target = await db.get(AcquisitionTarget, item.target_id)
        members.append(
            DownloadMemberView(
                selection_id=item.id,
                intent_id=item.intent_id,
                work_title=item.frozen["work_title"],
                medium=item.frozen["requirements"]["medium"],
                state=item.state,
                target_state=target.state,
                message=target.message,
                fulfillment=await fulfillment_view(db, user, row, item),
                join_operation_id=memberships[item.id].join_operation_id,
            )
        )
    confirmed = next(
        (item.fulfillment for item in members if item.selection_id == selection.id), None
    )
    visible = {str(item.selection_id) for item in members}
    continuations = []
    for item in await db.scalars(
        select(AutomaticImportContinuation)
        .where(AutomaticImportContinuation.attempt_id == row.id)
        .order_by(AutomaticImportContinuation.created_at, AutomaticImportContinuation.id)
    ):
        ids = [value for value in item.evidence["authorized_selection_ids"] if value in visible]
        if ids:
            state, detail = item.state, item.message
            joined = [member for member in members if str(member.selection_id) in ids]
            if joined and all(
                member.fulfillment and member.fulfillment.available_now for member in joined
            ):
                state, detail = "complete", "Joined books are confirmed in your library"
            elif item.import_run_id and await db.scalar(
                select(ImportEntry.id)
                .where(
                    ImportEntry.run_id == item.import_run_id,
                    ImportEntry.state.in_(["held", "cancel-held"]),
                )
                .limit(1)
            ):
                state, detail = (
                    "attention",
                    "Additional imports need administrator file review; the download is preserved",
                )
            continuations.append(
                ImportContinuationView(id=item.id, state=state, message=detail, selection_ids=ids)
            )
    from app.domain.download_recovery import history

    recoveries = list(
        await db.scalars(
            select(DownloadRecovery).where(
                DownloadRecovery.attempt_id == row.id,
                DownloadRecovery.selection_id.in_(
                    select(AcquisitionSelection.id).where(AcquisitionSelection.owner_id == user.id)
                ),
            )
        )
    )
    imported = await db.scalar(
        select(DownloadFulfillment).where(
            DownloadFulfillment.attempt_id == row.id,
            DownloadFulfillment.target_id == selection.target_id,
            DownloadFulfillment.import_entry_id.is_not(None),
        )
    )
    return AttemptView(
        attempt_chain=await history(db, selection),
        recoveries=[
            RecoveryStatusView(
                id=r.id,
                state=r.state,
                reason=r.reason,
                message=r.message,
                cleanup=r.evidence.get("cleanup_state", "pending"),
                can_approve=user.role == "admin" and r.state == "approval",
            )
            for r in recoveries
        ],
        can_report_problem=user.role != "viewer" and row.state == "complete" and not recoveries,
        imported_asset_id=imported.asset_id if imported else None,
        id=row.id,
        created_at=row.created_at,
        selection_id=selection.id,
        operation_id=row.operation_id,
        state=row.state,
        work_title=selection.frozen["work_title"],
        release_title=selection.frozen["release"]["title"],
        source=selection.frozen["release"].get("source"),
        message=message,
        external_may_exist=row.external_may_exist,
        can_cancel=not row.external_may_exist and row.state != "cancelled",
        can_recheck=not recoveries
        and row.state != "cancelled"
        and not repairing
        and (not row.lease_until or row.lease_until <= datetime.now(UTC))
        and (not row.next_check_at or row.next_check_at <= datetime.now(UTC)),
        progress=(row.observation or {}).get("progress"),
        inspection_id=inspection.id
        if inspection and inspection.owner_id == user.id and user.role == "admin"
        else None,
        fulfillment=confirmed,
        repair=RepairView.model_validate(repair) if repair else None,
        can_repair=needs_review,
        members=members,
        import_continuations=continuations,
    )


async def fulfillment_view(db, user, row, selection):
    fulfillment = await db.scalar(
        select(DownloadFulfillment).where(
            DownloadFulfillment.attempt_id == row.id,
            DownloadFulfillment.target_id == selection.target_id,
        )
    )
    confirmed = None
    if fulfillment:
        intent = await db.get(AcquisitionIntent, selection.intent_id)
        target = await db.get(AcquisitionTarget, selection.target_id)
        outcomes = await assess(
            db, user, intent.work_id, RequestSpec.model_validate(intent.specification)
        )
        confirmed = FulfillmentView(
            confirmed_at=fulfillment.created_at,
            basis=fulfillment.evidence["basis"],
            available_now=any(
                item["slot"] == target.slot and item["state"] == "satisfied" for item in outcomes
            ),
        )
    return confirmed


@router.post("", response_model=AttemptView, status_code=202)
async def start(
    body: StartInput,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    try:
        row = await downloads.start(
            db,
            user,
            body.selection_id,
            idempotency_key,
            additional_selection_ids=body.additional_selection_ids,
        )
    except AdapterError as error:
        raise adapter_http_error(error) from error
    result = await view(db, user, row, await db.get(AcquisitionSelection, row.selection_id))
    await db.commit()
    return result


@router.get("", response_model=AttemptPage)
async def listing(
    user: CurrentUser,
    db: Database,
    selection_id: UUID | None = None,
    work_id: UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    where = [DownloadAttempt.owner_id == user.id]
    if work_id:
        selected = (
            select(AcquisitionSelection.id)
            .join(AcquisitionIntent, AcquisitionIntent.id == AcquisitionSelection.intent_id)
            .where(AcquisitionIntent.work_id.in_(family_ids(work_id)))
        )
        where.append(
            or_(
                DownloadAttempt.selection_id.in_(selected),
                DownloadAttempt.id.in_(
                    select(DownloadMembership.attempt_id).where(
                        DownloadMembership.selection_id.in_(selected)
                    )
                ),
            )
        )
    if selection_id:
        where.append(
            DownloadAttempt.id.in_(
                select(DownloadMembership.attempt_id).where(
                    DownloadMembership.selection_id == selection_id
                )
            )
        )
    rows = await db.scalars(
        select(DownloadAttempt)
        .where(*where)
        .order_by(DownloadAttempt.created_at.desc(), DownloadAttempt.id)
        .offset(offset)
        .limit(limit)
    )
    rows = list(rows)
    selections = {
        item.id: item
        for item in await db.scalars(
            select(AcquisitionSelection).where(
                AcquisitionSelection.id.in_([row.selection_id for row in rows])
            )
        )
    }
    items = []
    for row in rows:
        selection = selections[row.selection_id]
        if work_id or selection_id:
            for member in await download_memberships.for_attempt(db, row.id):
                if member.owner_id != user.id:
                    continue
                if selection_id and member.id != selection_id:
                    continue
                if work_id and not await db.scalar(
                    select(AcquisitionIntent.id).where(
                        AcquisitionIntent.id == member.intent_id,
                        AcquisitionIntent.work_id.in_(family_ids(work_id)),
                    )
                ):
                    continue
                selection = member
                break
        items.append(await view(db, user, row, selection))
    return AttemptPage(
        items=items,
        offset=offset,
        limit=limit,
        total=await db.scalar(select(func.count()).select_from(DownloadAttempt).where(*where)),
    )


@router.get("/{attempt_id}", response_model=AttemptView)
async def detail(attempt_id: UUID, user: CurrentUser, db: Database, work_id: UUID | None = None):
    row = await downloads.owned_attempt(db, user, attempt_id)
    selection = await db.get(AcquisitionSelection, row.selection_id)
    if work_id:
        selection = await db.scalar(
            select(AcquisitionSelection)
            .join(DownloadMembership)
            .join(AcquisitionIntent, AcquisitionIntent.id == AcquisitionSelection.intent_id)
            .where(
                DownloadMembership.attempt_id == row.id,
                AcquisitionSelection.owner_id == user.id,
                AcquisitionIntent.work_id.in_(family_ids(work_id)),
            )
        )
        if not selection:
            raise HTTPException(404, "Book is not a member of this download")
    return await view(db, user, row, selection)


@router.delete("/{attempt_id}", response_model=AttemptView)
async def cancel(attempt_id: UUID, user: Member, db: Database):
    row = await downloads.cancel(db, user, attempt_id)
    result = await view(db, user, row, await db.get(AcquisitionSelection, row.selection_id))
    await db.commit()
    return result


@router.post("/{attempt_id}/recheck", response_model=AttemptView, status_code=202)
async def recheck(attempt_id: UUID, user: Member, db: Database):
    row = await downloads.recheck(db, user, attempt_id)
    result = await view(db, user, row, await db.get(AcquisitionSelection, row.selection_id))
    await db.commit()
    return result


@router.get("/{attempt_id}/repair-preview", response_model=RepairPreview)
async def repair_preview(attempt_id: UUID, user: Admin, db: Database):
    return await repairs.preview(db, user, attempt_id)


@router.post("/{attempt_id}/repairs", response_model=RepairView, status_code=202)
async def repair_download(
    attempt_id: UUID,
    body: RepairInput,
    user: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    try:
        row = await repairs.start(db, user, attempt_id, body.revision, idempotency_key)
    except AdapterError as error:
        raise adapter_http_error(error) from error
    result = RepairView.model_validate(row)
    await db.commit()
    return result
