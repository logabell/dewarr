from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import exists, select

from app.api.dependencies import Admin, Database
from app.config import get_settings
from app.db.models import (
    AcquisitionSelection,
    AutomaticImport,
    DownloadAttempt,
    DownloadInspection,
    FrozenImportPlan,
    ImportDestination,
    ImportEntry,
    ImportRun,
    Operation,
)
from app.domain.operations import transaction_lock
from app.importing.file_editions import attach_file_edition
from app.importing.filesystem import relative_parts
from app.importing.inspection import InspectionSnapshot
from app.importing.naming import (
    StrictModel,
)
from app.importing.planning import FreezeInput, FrozenPlanView, assert_admin, owned_inspection
from app.importing.planning import freeze_plan as plan_import_command
from app.importing.storage import import_sources
from app.jobs.queue import enqueue

router = APIRouter(prefix="/organization", tags=["organization"])


class InspectInput(StrictModel):
    source_key: str = Field(pattern=r"^[a-z0-9_-]{1,60}$")
    relative_path: str = Field(min_length=1, max_length=1024)
    completed_download: Literal[True]

    @model_validator(mode="after")
    def valid_path(self):
        relative_parts(self.relative_path)
        return self


class FileEditionInput(StrictModel):
    work_id: UUID
    group_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    grouping_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class FileEditionView(BaseModel):
    work_id: UUID
    version_id: UUID
    medium: str
    title: str | None
    created: bool


class DownloadContext(BaseModel):
    attempt_id: UUID
    work_id: UUID
    title: str
    authors: list[str]
    cover_url: str | None
    medium: str
    destination: str | None
    mode: str | None
    state: str
    message: str
    can_retry: bool = False


class InspectionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    operation_id: UUID
    source_key: str
    relative_path: str
    state: str
    message: str
    snapshot: InspectionSnapshot | None = None
    plan_id: UUID | None = None
    download: DownloadContext | None = None


@router.get("/download-roots", response_model=list[str])
async def download_roots(admin: Admin, db: Database):
    return sorted(await import_sources(db))


@router.post("/inspections", status_code=202, response_model=InspectionView)
async def create_inspection(
    body: InspectInput,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Inspection is paused for recovery")
    root = (await import_sources(db)).get(body.source_key)
    if not root:
        raise HTTPException(422, "Select a download root configured on the worker")
    await transaction_lock(db, f"operation:{admin.id}:{idempotency_key}")
    await assert_admin(db, admin.id)
    payload = {**body.model_dump(), "source_path": str(root)}
    operation = await db.scalar(
        select(Operation).where(
            Operation.owner_id == admin.id, Operation.idempotency_key == idempotency_key
        )
    )
    if operation:
        if operation.kind != "organization.inspect" or operation.payload != payload:
            raise HTTPException(409, "This operation key was already used for another command")
        return await db.scalar(
            select(DownloadInspection).where(DownloadInspection.operation_id == operation.id)
        )
    operation = Operation(
        owner_id=admin.id,
        kind="organization.inspect",
        idempotency_key=idempotency_key,
        payload=payload,
    )
    db.add(operation)
    await db.flush()
    row = DownloadInspection(
        owner_id=admin.id,
        operation_id=operation.id,
        source_key=body.source_key,
        source_path=str(root),
        relative_path=body.relative_path,
    )
    db.add(row)
    operation.job_id = await enqueue(db, "organization.inspect", operation_id=str(operation.id))
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/inspections", response_model=list[InspectionView])
async def inspections(admin: Admin, db: Database, offset: int = Query(0, ge=0)):
    # Summary list does not load potentially large file snapshots.
    rows = (
        (
            await db.execute(
                select(
                    DownloadInspection.id,
                    DownloadInspection.created_at,
                    DownloadInspection.operation_id,
                    DownloadInspection.source_key,
                    DownloadInspection.relative_path,
                    DownloadInspection.state,
                    DownloadInspection.message,
                )
                .where(DownloadInspection.owner_id == admin.id)
                .order_by(DownloadInspection.created_at.desc(), DownloadInspection.id)
                .offset(offset)
                .limit(25)
            )
        )
        .mappings()
        .all()
    )
    return [InspectionView.model_validate(row) for row in rows]


@router.get("/inspections/{inspection_id}", response_model=InspectionView)
async def inspection(inspection_id: UUID, admin: Admin, db: Database):
    row = await owned_inspection(db, admin.id, inspection_id)
    value = InspectionView.model_validate(row)
    plan = await db.scalar(
        select(FrozenImportPlan)
        .where(FrozenImportPlan.inspection_id == row.id, FrozenImportPlan.owner_id == admin.id)
        .order_by(
            exists(
                select(ImportRun.id)
                .join(ImportEntry)
                .where(
                    ImportRun.plan_id == FrozenImportPlan.id,
                    ImportEntry.state != "cancelled",
                )
            ).desc(),
            FrozenImportPlan.created_at.desc(),
            FrozenImportPlan.id.desc(),
        )
        .limit(1)
    )
    value.plan_id = plan.id if plan else None
    attempt = await db.scalar(
        select(DownloadAttempt).where(DownloadAttempt.inspection_id == row.id)
    )
    if not attempt:
        return value
    from app.domain.work_graph import canonical_work

    selection = await db.get(AcquisitionSelection, attempt.selection_id)
    work = await canonical_work(db, UUID(selection.frozen["origin_work_id"]))
    destination = await db.get(ImportDestination, selection.destination_id)
    automatic = await db.scalar(
        select(AutomaticImport).where(AutomaticImport.inspection_id == row.id)
    )
    state = automatic.state if automatic else "review"
    message = automatic.message if automatic else "Confirm the files for your requested book."
    if plan:
        entries = list(
            await db.scalars(
                select(ImportEntry)
                .join(ImportRun)
                .where(ImportRun.plan_id == plan.id)
                .order_by(ImportEntry.created_at, ImportEntry.id)
            )
        )
        if entries:
            blocked = next(
                (item for item in entries if item.state in {"held", "cancel-held"}), None
            )
            if blocked:
                state, message = "held", blocked.message
            elif all(item.state in {"confirmed", "skipped"} for item in entries):
                state, message = "complete", "Imported and available in your library."
            elif all(item.state == "cancelled" for item in entries):
                state, message = "cancelled", "Import stopped. Downloaded files are preserved."
            else:
                current = next(
                    (
                        item
                        for item in entries
                        if item.state not in {"confirmed", "skipped", "cancelled"}
                    ),
                    entries[0],
                )
                state, message = current.state, current.message
    value.download = DownloadContext(
        attempt_id=attempt.id,
        work_id=work.id,
        title=work.title,
        authors=work.authors,
        cover_url=work.cover_url,
        medium=selection.frozen["requirements"]["medium"],
        destination=destination.backend_path if destination else None,
        mode=destination.mode if destination else None,
        state=state,
        message=message,
        can_retry=bool(
            automatic
            and automatic.state == "held"
            and not automatic.import_run_id
            and not automatic.evidence.get("release_rejection")
            and not get_settings().recovery_mode
        ),
    )
    return value


@router.post("/inspections/{inspection_id}/retry", response_model=InspectionView, status_code=202)
async def retry_download_import(inspection_id: UUID, admin: Admin, db: Database):
    row = await owned_inspection(db, admin.id, inspection_id)
    attempt = await db.scalar(
        select(DownloadAttempt).where(DownloadAttempt.inspection_id == row.id)
    )
    if not attempt:
        raise HTTPException(409, "This inspection is not linked to a completed download")
    from app.importing.automatic import retry_held

    if not await retry_held(db, attempt):
        raise HTTPException(
            409, "This import already has a plan or needs file review; refresh its status"
        )
    await db.commit()
    return await inspection(inspection_id, admin, db)


@router.post(
    "/inspections/{inspection_id}/editions",
    response_model=FileEditionView,
)
async def create_file_edition(
    inspection_id: UUID, body: FileEditionInput, admin: Admin, db: Database
):
    version, created = await attach_file_edition(
        db, admin, inspection_id, body.work_id, body.group_key, body.grouping_revision
    )
    await db.commit()
    return FileEditionView(
        work_id=version.work_id,
        version_id=version.id,
        medium=version.medium,
        title=version.title,
        created=created,
    )


@router.post("/inspections/{inspection_id}/plans", response_model=FrozenPlanView, status_code=201)
async def freeze_plan(inspection_id: UUID, body: FreezeInput, admin: Admin, db: Database):
    row = await plan_import_command(db, admin, inspection_id, body)
    await db.commit()
    return row


@router.get("/plans/{plan_id}", response_model=FrozenPlanView)
async def frozen_plan(plan_id: UUID, admin: Admin, db: Database):
    row = await db.scalar(
        select(FrozenImportPlan).where(
            FrozenImportPlan.id == plan_id, FrozenImportPlan.owner_id == admin.id
        )
    )
    if not row:
        raise HTTPException(404, "Import plan not found")
    return row
