from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.dependencies import Admin, Database
from app.config import get_settings
from app.db.models import (
    DownloadAttempt,
    ImportEntry,
    Operation,
    RecoveryFinding,
    RecoveryQueueFence,
    RecoveryScan,
)
from app.domain.recovery_access import AccessChoice
from app.domain.recovery_connections import ConnectionChoice
from app.domain.recovery_sources import SourceChoice
from app.recovery import active_restore, restore_pending

router = APIRouter(prefix="/recovery", tags=["recovery"])


class QueueFenceView(BaseModel):
    historical_jobs: int
    subjects: dict[str, int]
    approvals_protected: bool


class RecoveryView(BaseModel):
    queue_fence: QueueFenceView | None = None
    paused: bool
    backup_id: UUID | None
    restored_at: datetime | None
    downloads: dict[str, int]
    imports: dict[str, int]
    resume_available: bool = False
    latest_scan: "ScanView | None" = None
    latest_access_reconciliation: "AccessReconciliationView | None" = None
    latest_connection_reconciliation: "ConnectionReconciliationView | None" = None
    latest_source_reconciliation: "SourceReconciliationView | None" = None
    latest_reconciliation: "ReconciliationView | None" = None
    latest_command_reconciliation: "CommandReconciliationView | None" = None
    latest_outbound_reconciliation: "OutboundReconciliationView | None" = None
    latest_list_reconciliation: "ListReconciliationView | None" = None
    latest_publication_reconciliation: "PublicationReconciliationView | None" = None
    latest_inventory_reconciliation: "InventoryReconciliationView | None" = None


class ScanView(BaseModel):
    id: UUID
    state: str
    created_at: datetime
    finished_at: datetime | None
    summary: dict
    message: str


async def scan_view(db, scan):
    operation = await db.get(Operation, scan.operation_id)
    return ScanView(
        id=scan.id,
        state=scan.state,
        created_at=scan.created_at,
        finished_at=scan.finished_at,
        summary=scan.summary,
        message=operation.message,
    )


@router.get("", response_model=RecoveryView)
async def review(admin: Admin, db: Database):
    checkpoint = await active_restore(db)
    latest = (
        await db.scalar(
            select(RecoveryScan)
            .where(RecoveryScan.checkpoint_id == checkpoint.id)
            .order_by(RecoveryScan.created_at.desc(), RecoveryScan.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    latest_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.reconcile",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    inventory_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.inventory",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    command_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.commands",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    outbound_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.outbound",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    list_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.lists",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    publication_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.publication",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    access_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.access",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    connection_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.connections",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    source_review = (
        await db.scalar(
            select(Operation)
            .where(
                Operation.kind == "recovery.sources",
                Operation.payload["checkpoint_id"].astext == str(checkpoint.id),
                Operation.owner_id == admin.id,
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        if checkpoint
        else None
    )
    fence = await db.get(RecoveryQueueFence, checkpoint.id) if checkpoint else None
    return RecoveryView(
        latest_source_reconciliation=await source_reconciliation_view(db, source_review)
        if source_review
        else None,
        latest_connection_reconciliation=connection_reconciliation_view(connection_review)
        if connection_review
        else None,
        latest_access_reconciliation=access_reconciliation_view(access_review)
        if access_review
        else None,
        queue_fence=QueueFenceView(
            historical_jobs=fence.job_count,
            subjects=fence.subject_counts,
            approvals_protected=fence.approval_version == 1,
        )
        if fence
        else None,
        latest_command_reconciliation=command_reconciliation_view(command_review)
        if command_review
        else None,
        latest_outbound_reconciliation=outbound_reconciliation_view(outbound_review)
        if outbound_review
        else None,
        latest_list_reconciliation=list_reconciliation_view(list_review) if list_review else None,
        latest_publication_reconciliation=publication_reconciliation_view(publication_review)
        if publication_review
        else None,
        latest_inventory_reconciliation=inventory_reconciliation_view(inventory_review)
        if inventory_review
        else None,
        latest_reconciliation=reconciliation_view(latest_review) if latest_review else None,
        paused=get_settings().recovery_mode or await restore_pending(db),
        backup_id=checkpoint.backup_id if checkpoint else None,
        restored_at=checkpoint.created_at if checkpoint else None,
        latest_scan=await scan_view(db, latest) if latest else None,
        downloads=dict(
            (
                await db.execute(
                    select(DownloadAttempt.state, func.count()).group_by(DownloadAttempt.state)
                )
            ).all()
        ),
        imports=dict(
            (
                await db.execute(
                    select(ImportEntry.state, func.count()).group_by(ImportEntry.state)
                )
            ).all()
        ),
    )


@router.post("/scans", response_model=ScanView, status_code=202)
async def begin_scan(
    admin: Admin, db: Database, idempotency_key: str = Header(min_length=8, max_length=200)
):
    from app.domain.recovery_scans import ScanHeld, start

    checkpoint = await active_restore(db)
    if not checkpoint or checkpoint.operator_id != admin.id:
        raise HTTPException(409, "An active restore checkpoint is required")
    try:
        scan = await start(db, checkpoint, idempotency_key)
    except ScanHeld as error:
        raise HTTPException(409, str(error)) from None
    await db.commit()
    return await scan_view(db, scan)


class FindingView(BaseModel):
    id: UUID
    domain: str
    state: str
    title: str
    message: str
    entity_id: UUID | None
    evidence: dict
    has_evidence: bool


class FindingsPage(BaseModel):
    scan: ScanView
    items: list[FindingView]
    total: int
    next_offset: int | None


@router.get("/scans/{scan_id}", response_model=FindingsPage)
async def findings(
    scan_id: UUID,
    admin: Admin,
    db: Database,
    offset: int = Query(0, ge=0, le=30000),
    domain: str | None = Query(None, pattern="^(downloads|library|lists|files|review)$"),
):
    checkpoint = await active_restore(db)
    scan = await db.get(RecoveryScan, scan_id)
    if (
        not checkpoint
        or checkpoint.operator_id != admin.id
        or not scan
        or scan.checkpoint_id != checkpoint.id
    ):
        raise HTTPException(404, "Recovery observation not found")
    conditions = [RecoveryFinding.scan_id == scan.id]
    if domain:
        conditions.append(RecoveryFinding.domain == domain)
    total = await db.scalar(select(func.count()).select_from(RecoveryFinding).where(*conditions))
    summary_fields = [
        "external_id",
        "external_item_id",
        "medium",
        "integration_id",
        "saved_state",
        "source",
        "failure",
        "complete",
        "total_transfers",
        "relevant_transfers",
        "unrelated_transfers",
    ]
    summary = func.jsonb_strip_nulls(
        func.jsonb_build_object(
            *[value for key in summary_fields for value in (key, RecoveryFinding.evidence[key])]
        )
    )
    columns = [
        getattr(RecoveryFinding, key)
        for key in FindingView.model_fields
        if key not in {"evidence", "has_evidence"}
    ]
    rows = (
        (
            await db.execute(
                select(
                    *columns,
                    summary.label("evidence"),
                    (RecoveryFinding.evidence != {}).label("has_evidence"),
                )
                .where(*conditions)
                .order_by(RecoveryFinding.position)
                .offset(offset)
                .limit(50)
            )
        )
        .mappings()
        .all()
    )
    return FindingsPage(
        scan=await scan_view(db, scan),
        items=[FindingView(**row) for row in rows],
        total=total,
        next_offset=offset + 50 if offset + 50 < total else None,
    )


@router.get("/scans/{scan_id}/findings/{finding_id}", response_model=FindingView)
async def finding_detail(scan_id: UUID, finding_id: UUID, admin: Admin, db: Database):
    checkpoint = await active_restore(db)
    scan = await db.get(RecoveryScan, scan_id)
    if (
        not checkpoint
        or checkpoint.operator_id != admin.id
        or not scan
        or scan.checkpoint_id != checkpoint.id
    ):
        raise HTTPException(404, "Recovery observation not found")
    row = await db.get(RecoveryFinding, finding_id)
    if not row or row.scan_id != scan.id:
        raise HTTPException(404, "Recovery finding not found")
    return FindingView(
        **{key: getattr(row, key) for key in FindingView.model_fields if key != "has_evidence"},
        has_evidence=bool(row.evidence),
    )


class ReconciliationItemView(BaseModel):
    finding_id: UUID
    attempt_id: UUID
    title: str
    saved_state: str
    saved_external_may_exist: bool
    observed_state: str
    external_id: str


class ReconciliationView(BaseModel):
    id: UUID
    scan_id: UUID
    status: str
    message: str
    created_at: datetime
    revision: str
    expires_at: datetime
    items: list[ReconciliationItemView]
    applied_at: datetime | None
    results: list[dict]


def reconciliation_view(operation):
    return ReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[ReconciliationItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
    )


class ReconciliationRequest(BaseModel):
    scan_id: UUID
    finding_ids: list[UUID] = Field(min_length=1, max_length=100)


class ReconciliationAcceptance(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


async def checkpoint_for(db, admin):
    checkpoint = await active_restore(db)
    if not checkpoint or checkpoint.operator_id != admin.id:
        raise HTTPException(409, "An active restore checkpoint is required")
    return checkpoint


@router.post("/reconciliations", response_model=ReconciliationView, status_code=201)
async def prepare_reconciliation(
    body: ReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import prepare

    operation = await prepare(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        body.scan_id,
        body.finding_ids,
        idempotency_key,
    )
    await db.commit()
    return reconciliation_view(operation)


@router.get("/reconciliations/{identifier}", response_model=ReconciliationView)
async def get_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_reconciliation import load

    return reconciliation_view(
        await load(db, identifier, await checkpoint_for(db, admin), admin.id)
    )


@router.post(
    "/reconciliations/{identifier}/accept", response_model=ReconciliationView, status_code=202
)
async def accept_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import accept

    operation = await accept(
        db, await checkpoint_for(db, admin), admin.id, identifier, body.revision, idempotency_key
    )
    await db.commit()
    return reconciliation_view(operation)


class InventoryReconciliationItemView(BaseModel):
    finding_id: UUID
    integration_id: UUID
    title: str
    summary: dict[str, int]


class InventoryReconciliationView(ReconciliationView):
    items: list[InventoryReconciliationItemView]


def inventory_reconciliation_view(operation):
    return InventoryReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[InventoryReconciliationItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
    )


@router.post(
    "/inventory-reconciliations", response_model=InventoryReconciliationView, status_code=201
)
async def prepare_inventory_reconciliation(
    body: ReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_inventory import prepare

    operation = await prepare(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        body.scan_id,
        body.finding_ids,
        idempotency_key,
    )
    await db.commit()
    return inventory_reconciliation_view(operation)


@router.get("/inventory-reconciliations/{identifier}", response_model=InventoryReconciliationView)
async def get_inventory_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_reconciliation import INVENTORY_KIND, load

    return inventory_reconciliation_view(
        await load(
            db,
            identifier,
            await checkpoint_for(db, admin),
            admin.id,
            kind=INVENTORY_KIND,
        )
    )


@router.post(
    "/inventory-reconciliations/{identifier}/accept",
    response_model=InventoryReconciliationView,
    status_code=202,
)
async def accept_inventory_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import INVENTORY_KIND, accept

    operation = await accept(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        identifier,
        body.revision,
        idempotency_key,
        kind=INVENTORY_KIND,
    )
    await db.commit()
    return inventory_reconciliation_view(operation)


class PublicationReconciliationItemView(BaseModel):
    finding_id: UUID
    entry_id: UUID
    title: str
    medium: str
    folder: str
    saved_state: str
    outcome: str
    reason: str


class PublicationReconciliationView(ReconciliationView):
    items: list[PublicationReconciliationItemView]


def publication_reconciliation_view(operation):
    return PublicationReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[PublicationReconciliationItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
    )


@router.post(
    "/publication-reconciliations", response_model=PublicationReconciliationView, status_code=201
)
async def prepare_publication_reconciliation(
    body: ReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_publication import prepare

    operation = await prepare(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        body.scan_id,
        body.finding_ids,
        idempotency_key,
    )
    await db.commit()
    return publication_reconciliation_view(operation)


@router.get(
    "/publication-reconciliations/{identifier}", response_model=PublicationReconciliationView
)
async def get_publication_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_reconciliation import PUBLICATION_KIND, load

    return publication_reconciliation_view(
        await load(
            db,
            identifier,
            await checkpoint_for(db, admin),
            admin.id,
            kind=PUBLICATION_KIND,
        )
    )


@router.post(
    "/publication-reconciliations/{identifier}/accept",
    response_model=PublicationReconciliationView,
    status_code=202,
)
async def accept_publication_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import PUBLICATION_KIND, accept

    operation = await accept(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        identifier,
        body.revision,
        idempotency_key,
        kind=PUBLICATION_KIND,
    )
    await db.commit()
    return publication_reconciliation_view(operation)


class ListReconciliationItemView(BaseModel):
    finding_id: UUID
    subscription_id: UUID
    list_id: UUID
    title: str
    provider: str
    complete: bool
    summary: dict[str, int]
    pause_acquisition: bool
    pause_writeback: bool


class ListReconciliationView(ReconciliationView):
    items: list[ListReconciliationItemView]


def list_reconciliation_view(operation):
    return ListReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[ListReconciliationItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
    )


@router.post("/list-reconciliations", response_model=ListReconciliationView, status_code=201)
async def prepare_list_reconciliation(
    body: ReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_lists import prepare

    operation = await prepare(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        body.scan_id,
        body.finding_ids,
        idempotency_key,
    )
    await db.commit()
    return list_reconciliation_view(operation)


@router.get("/list-reconciliations/{identifier}", response_model=ListReconciliationView)
async def get_list_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_reconciliation import LIST_KIND, load

    return list_reconciliation_view(
        await load(
            db,
            identifier,
            await checkpoint_for(db, admin),
            admin.id,
            kind=LIST_KIND,
        )
    )


@router.post(
    "/list-reconciliations/{identifier}/accept",
    response_model=ListReconciliationView,
    status_code=202,
)
async def accept_list_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import LIST_KIND, accept

    operation = await accept(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        identifier,
        body.revision,
        idempotency_key,
        kind=LIST_KIND,
    )
    await db.commit()
    return list_reconciliation_view(operation)


class OutboundReconciliationItemView(BaseModel):
    finding_id: UUID
    operation_id: UUID
    title: str
    desired: bool
    saved_status: str
    pending_attempt: bool
    outcome: str
    message: str


class OutboundReconciliationView(ReconciliationView):
    items: list[OutboundReconciliationItemView]


def outbound_reconciliation_view(operation):
    return OutboundReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[OutboundReconciliationItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
    )


@router.post(
    "/outbound-reconciliations", response_model=OutboundReconciliationView, status_code=201
)
async def prepare_outbound_reconciliation(
    body: ReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_outbound import prepare

    operation = await prepare(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        body.scan_id,
        body.finding_ids,
        idempotency_key,
    )
    await db.commit()
    return outbound_reconciliation_view(operation)


@router.get("/outbound-reconciliations/{identifier}", response_model=OutboundReconciliationView)
async def get_outbound_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_reconciliation import OUTBOUND_KIND, load

    return outbound_reconciliation_view(
        await load(
            db,
            identifier,
            await checkpoint_for(db, admin),
            admin.id,
            kind=OUTBOUND_KIND,
        )
    )


@router.post(
    "/outbound-reconciliations/{identifier}/accept",
    response_model=OutboundReconciliationView,
    status_code=202,
)
async def accept_outbound_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import OUTBOUND_KIND, accept

    operation = await accept(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        identifier,
        body.revision,
        idempotency_key,
        kind=OUTBOUND_KIND,
    )
    await db.commit()
    return outbound_reconciliation_view(operation)


class CommandReconciliationItemView(BaseModel):
    finding_id: UUID
    entity_id: UUID
    entity_type: str
    title: str
    kind: str
    saved_state: str
    action: str


class CommandReconciliationView(ReconciliationView):
    items: list[CommandReconciliationItemView]


def command_reconciliation_view(operation):
    return CommandReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[CommandReconciliationItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
    )


@router.post("/command-reconciliations", response_model=CommandReconciliationView, status_code=201)
async def prepare_command_reconciliation(
    body: ReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_commands import prepare

    operation = await prepare(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        body.scan_id,
        body.finding_ids,
        idempotency_key,
    )
    await db.commit()
    return command_reconciliation_view(operation)


@router.get("/command-reconciliations/{identifier}", response_model=CommandReconciliationView)
async def get_command_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_reconciliation import COMMAND_KIND, load

    return command_reconciliation_view(
        await load(
            db,
            identifier,
            await checkpoint_for(db, admin),
            admin.id,
            kind=COMMAND_KIND,
        )
    )


@router.post(
    "/command-reconciliations/{identifier}/accept",
    response_model=CommandReconciliationView,
    status_code=202,
)
async def accept_command_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import COMMAND_KIND, accept

    operation = await accept(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        identifier,
        body.revision,
        idempotency_key,
        kind=COMMAND_KIND,
    )
    await db.commit()
    return command_reconciliation_view(operation)


class AccessReconciliationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    scan_id: UUID
    changes: list[AccessChoice] = Field(min_length=1, max_length=100)


class AccessStateView(BaseModel):
    active: bool
    role: str
    can_automate: bool
    permissions: int = 0
    library_ids: list[UUID]


class AccessLibraryView(BaseModel):
    id: UUID
    name: str


class AccessItemView(BaseModel):
    finding_id: UUID
    user_id: UUID
    username: str
    operator: bool
    before: AccessStateView
    after: AccessStateView
    libraries: list[AccessLibraryView]


class AccessReconciliationView(ReconciliationView):
    items: list[AccessItemView]


def access_reconciliation_view(operation):
    return AccessReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[AccessItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
    )


@router.post("/access-reconciliations", response_model=AccessReconciliationView, status_code=201)
async def prepare_access_reconciliation(
    body: AccessReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_access import prepare

    operation = await prepare(
        db, await checkpoint_for(db, admin), admin.id, body.scan_id, body.changes, idempotency_key
    )
    await db.commit()
    return access_reconciliation_view(operation)


@router.get("/access-reconciliations/{identifier}", response_model=AccessReconciliationView)
async def get_access_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_reconciliation import ACCESS_KIND, load

    return access_reconciliation_view(
        await load(db, identifier, await checkpoint_for(db, admin), admin.id, kind=ACCESS_KIND)
    )


@router.post(
    "/access-reconciliations/{identifier}/accept",
    response_model=AccessReconciliationView,
    status_code=202,
)
async def accept_access_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import ACCESS_KIND, accept

    operation = await accept(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        identifier,
        body.revision,
        idempotency_key,
        kind=ACCESS_KIND,
    )
    await db.commit()
    return access_reconciliation_view(operation)


class RecoveryPathMapping(BaseModel):
    download_root: str
    source_key: str
    worker_path: str


class RecoveryABSSettings(BaseModel):
    kind: Literal["audiobookshelf"]
    name: str
    base_url: str
    enabled: bool
    has_credentials: bool
    public_url: str


class RecoveryQbitSettings(BaseModel):
    kind: Literal["qbittorrent"]
    name: str
    base_url: str
    enabled: bool
    has_credentials: bool
    save_path: str
    category: str
    mappings: list[RecoveryPathMapping]


class ConnectionRepairItemView(BaseModel):
    finding_id: UUID
    integration_id: UUID
    before: RecoveryABSSettings | RecoveryQbitSettings
    after: RecoveryABSSettings | RecoveryQbitSettings
    replace_credentials: bool


class ConnectionReconciliationView(ReconciliationView):
    items: list[ConnectionRepairItemView]


class ConnectionReconciliationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    scan_id: UUID
    changes: list[ConnectionChoice] = Field(min_length=1, max_length=10)


def connection_reconciliation_view(operation):
    return ConnectionReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[ConnectionRepairItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
    )


@router.post(
    "/connection-reconciliations", response_model=ConnectionReconciliationView, status_code=201
)
async def prepare_connection_reconciliation(
    body: ConnectionReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_connections import prepare

    operation = await prepare(
        db, await checkpoint_for(db, admin), admin.id, body.scan_id, body.changes, idempotency_key
    )
    await db.commit()
    return connection_reconciliation_view(operation)


@router.get("/connection-reconciliations/{identifier}", response_model=ConnectionReconciliationView)
async def get_connection_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_connections import KIND
    from app.domain.recovery_reconciliation import load

    return connection_reconciliation_view(
        await load(db, identifier, await checkpoint_for(db, admin), admin.id, kind=KIND)
    )


@router.post(
    "/connection-reconciliations/{identifier}/accept",
    response_model=ConnectionReconciliationView,
    status_code=202,
)
async def accept_connection_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_connections import KIND
    from app.domain.recovery_reconciliation import accept

    operation = await accept(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        identifier,
        body.revision,
        idempotency_key,
        kind=KIND,
    )
    await db.commit()
    return connection_reconciliation_view(operation)


class RecoverySourceSettings(BaseModel):
    source_key: Literal["mam", "prowlarr", "audiobookbay"]
    base_url: str
    enabled: bool
    proxy_url: str | None
    proxy_fallback_direct: bool
    has_credentials: bool
    has_proxy_credentials: bool
    excluded_indexers: list[int]
    metadata_downloader_id: UUID | None


class SourceRepairItemView(BaseModel):
    finding_id: UUID
    source_key: str
    before: RecoverySourceSettings
    after: RecoverySourceSettings
    replace_credentials: bool
    proxy_credentials: Literal["keep", "clear", "replace"]


class SourceVerificationView(BaseModel):
    id: UUID
    status: str
    message: str
    verified_at: datetime | None


class SourceReconciliationView(ReconciliationView):
    items: list[SourceRepairItemView]
    verification: SourceVerificationView | None


class SourceReconciliationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    scan_id: UUID
    change: SourceChoice


async def source_reconciliation_view(db, operation):
    proof = None
    test_id = next(
        (item["test_id"] for item in operation.payload.get("results", []) if item.get("test_id")),
        None,
    )
    if test_id:
        test = await db.get(Operation, UUID(test_id))
        if test and test.owner_id == operation.owner_id and test.kind == "recovery.source-test":
            proof = SourceVerificationView(
                id=test.id,
                status=test.status,
                message=test.message,
                verified_at=test.payload.get("verified_at"),
            )
    return SourceReconciliationView(
        id=operation.id,
        scan_id=operation.payload["command"]["scan_id"],
        status=operation.status,
        message=operation.message,
        created_at=operation.created_at,
        revision=operation.payload["revision"],
        expires_at=operation.payload["expires_at"],
        items=[SourceRepairItemView(**item) for item in operation.payload["items"]],
        applied_at=operation.payload.get("applied_at"),
        results=operation.payload.get("results", []),
        verification=proof,
    )


@router.post("/source-reconciliations", response_model=SourceReconciliationView, status_code=201)
async def prepare_source_reconciliation(
    body: SourceReconciliationRequest,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_sources import prepare

    operation = await prepare(
        db, await checkpoint_for(db, admin), admin.id, body.scan_id, body.change, idempotency_key
    )
    await db.commit()
    return await source_reconciliation_view(db, operation)


@router.get("/source-reconciliations/{identifier}", response_model=SourceReconciliationView)
async def get_source_reconciliation(identifier: UUID, admin: Admin, db: Database):
    from app.domain.recovery_reconciliation import load
    from app.domain.recovery_sources import KIND

    operation = await load(db, identifier, await checkpoint_for(db, admin), admin.id, kind=KIND)
    return await source_reconciliation_view(db, operation)


@router.post(
    "/source-reconciliations/{identifier}/accept",
    response_model=SourceReconciliationView,
    status_code=202,
)
async def accept_source_reconciliation(
    identifier: UUID,
    body: ReconciliationAcceptance,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.recovery_reconciliation import accept
    from app.domain.recovery_sources import KIND

    operation = await accept(
        db,
        await checkpoint_for(db, admin),
        admin.id,
        identifier,
        body.revision,
        idempotency_key,
        kind=KIND,
    )
    await db.commit()
    return await source_reconciliation_view(db, operation)
