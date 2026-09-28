from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.dependencies import Database, Member
from app.db.models import AcquisitionIntent, AcquisitionSelection, Operation
from app.domain import automatic_selection as automatic
from app.domain.acquisition import RequestSpec, validate_request
from app.domain.automatic_selection import AutomaticSelectionInput

router = APIRouter(prefix="/acquisition/automatic-selections", tags=["automatic-selection"])


class CoveredWork(BaseModel):
    id: UUID
    title: str
    authors: list[str]


class CoveredMember(BaseModel):
    work: CoveredWork
    files: list[str]


class PackCoverage(BaseModel):
    series_id: UUID
    series_generation: int
    series_name: str
    target_id: UUID
    evidence: str
    members: list[CoveredMember]


class CandidateDecision(BaseModel):
    result_id: UUID
    source: str
    title: str
    reasons: list[str]
    inspected: bool = False
    selected: bool = False
    coverage: PackCoverage | None = None


class PackExpansionView(BaseModel):
    state: Literal["review", "empty", "accepted"]
    message: str
    external_id: str | None = None
    request_id: UUID | None = None
    work_ids: list[UUID] = []


class AutomaticSelectionView(BaseModel):
    id: UUID
    status: str
    message: str
    inspections: int
    decisions: list[CandidateDecision]
    selection_id: UUID | None = None
    artifact_id: UUID | None = None
    download_when_ready: bool = False
    download_id: UUID | None = None
    pack_expansion: PackExpansionView | None = None


async def view(db, user, operation):
    intent = await db.get(AcquisitionIntent, UUID(operation.payload["command"]["intent_id"]))
    if not intent or intent.owner_id != user.id:
        raise HTTPException(404, "Request not found")
    await validate_request(
        db,
        user,
        intent.work_id,
        RequestSpec.model_validate(intent.specification),
        check_version_constraints=False,
    )
    selection_id = operation.payload.get("selection_id")
    selection = await db.get(AcquisitionSelection, UUID(selection_id)) if selection_id else None
    return AutomaticSelectionView(
        id=operation.id,
        status=operation.status,
        message=operation.message,
        inspections=len(operation.payload["inspected"]),
        decisions=[
            {
                **decision,
                "selected": bool(
                    selection
                    and selection.frozen.get("automatic_selection", {}).get("result_id")
                    == decision["result_id"]
                ),
            }
            for decision in operation.payload["decisions"]
        ],
        selection_id=selection_id,
        artifact_id=selection.artifact_id if selection else None,
        download_when_ready=operation.payload["command"].get("download_when_ready", False),
        download_id=operation.payload.get("download_id"),
        pack_expansion=operation.payload.get("pack_expansion"),
    )


@router.post("", response_model=AutomaticSelectionView, status_code=202)
async def create(
    body: AutomaticSelectionInput,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    operation = await automatic.begin(db, user, body, idempotency_key)
    response = await view(db, user, operation)
    await db.commit()
    return response


@router.get("/latest/{intent_id}/{slot}", response_model=AutomaticSelectionView | None)
async def latest(intent_id: UUID, slot: str, user: Member, db: Database):
    operation = await db.scalar(
        select(Operation)
        .where(
            Operation.owner_id == user.id,
            Operation.kind == automatic.KIND,
            Operation.payload["command"]["intent_id"].astext == str(intent_id),
            Operation.payload["command"]["slot"].astext == slot,
        )
        .order_by(Operation.created_at.desc(), Operation.id)
        .limit(1)
    )
    if not operation:
        return None
    operation = await automatic.owned(db, user, operation.id)
    response = await view(db, user, operation)
    await db.commit()
    return response


@router.get("/{operation_id}", response_model=AutomaticSelectionView)
async def detail(operation_id: UUID, user: Member, db: Database):
    operation = await automatic.owned(db, user, operation_id)
    response = await view(db, user, operation)
    await db.commit()
    return response


@router.post("/{operation_id}/cancel", response_model=AutomaticSelectionView)
async def cancel(operation_id: UUID, user: Member, db: Database):
    operation = await automatic.owned(db, user, operation_id)
    if operation.status == "completed":
        raise HTTPException(409, "Selection has completed; check its result or saved release")
    if operation.payload.get("pack_dispatch", {}).get("state") in {"waiting", "held"}:
        from app.domain.acquisition_selection import cancel as cancel_selection

        selection = await db.get(AcquisitionSelection, UUID(operation.payload["selection_id"]))
        if selection and selection.state == "prepared":
            await cancel_selection(db, user, selection)
        operation.payload = {
            **operation.payload,
            "pack_dispatch": {**operation.payload["pack_dispatch"], "state": "cancelled"},
        }
    automatic.finish(
        operation, "cancelled", "Automatic selection cancelled; no download was started"
    )
    response = await view(db, user, operation)
    await db.commit()
    return response
