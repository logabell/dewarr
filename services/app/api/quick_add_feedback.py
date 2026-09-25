"""Quick-add receipts with source-by-source feedback, including older operations."""

from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.adapters.source_releases import SOURCE_NAMES
from app.api.operations import OperationView
from app.db.models import Operation
from app.domain.selection_feedback import no_release_message, rejection_reasons


class QuickAddSourceCheck(BaseModel):
    slot: str
    source: str
    status: str
    message: str
    reasons: list[str] = Field(default_factory=list)
    candidates: int
    inspected: int


class QuickAddView(OperationView):
    request_id: UUID | None = None
    source_checks: list[QuickAddSourceCheck] = Field(default_factory=list)


async def quick_add_view(db, operation):
    children = list(
        await db.scalars(
            select(Operation)
            .where(
                Operation.owner_id == operation.owner_id,
                Operation.kind == "acquisition.auto-select",
                Operation.idempotency_key.startswith(f"quick-select:{operation.id}:"),
            )
            .order_by(Operation.created_at, Operation.id)
        )
    )
    checks = []
    for child in children:
        source = child.idempotency_key.rsplit(":", 1)[-1]
        payload = child.payload
        reasons = rejection_reasons(payload)
        message = child.message
        if child.status == "held" and message.startswith("No eligible release found"):
            message = no_release_message(payload)
        checks.append(
            QuickAddSourceCheck(
                slot=payload["command"]["slot"],
                source=SOURCE_NAMES.get(source, "Connected sources"),
                status=child.status,
                message=message,
                reasons=reasons,
                candidates=len(payload.get("decisions", [])),
                inspected=len(payload.get("inspected", [])),
            )
        )
    return QuickAddView(
        **OperationView.model_validate(operation).model_dump(),
        request_id=operation.payload.get("intent_id"),
        source_checks=checks,
    )
