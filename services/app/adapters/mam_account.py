"""Allowlisted MAM account data and store commands; see docs/mam-api/."""

import math
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.adapters.contracts import AdapterError, FailureKind

UploadAmount = Annotated[int, Field(strict=True, ge=50, le=100_000)] | Literal["max"]


class MAMAccount(BaseModel):
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    username: str
    uid: str
    classname: str | None = None
    ratio: str | None = None
    uploaded: str | None = None
    downloaded: str | None = None
    seedbonus: float | None = None
    vip_until: datetime | None = None


class MAMPurchase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_generation: int = Field(ge=0)
    kind: Literal["upload", "VIP", "wedges"]
    amount: UploadAmount | None = None

    @model_validator(mode="after")
    def purchase_fields(self):
        if (self.kind == "upload") != (self.amount is not None):
            raise ValueError("Upload requires an amount; VIP and wedges do not accept one")
        return self


class MAMPurchaseResult(BaseModel):
    status: Literal["completed", "rejected", "unknown"]
    message: str


def account_data(payload):
    from app.adapters.mam import integer, number, vip_until_from

    if not integer(payload.get("uid")) or not isinstance(payload.get("username"), str):
        raise AdapterError(
            FailureKind.AUTHENTICATION,
            "MAM did not confirm an authenticated account. Check mam_id and route.",
        )

    def display(key):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return str(value)
        return None

    points = number(payload.get("seedbonus"))
    return MAMAccount(
        username=payload["username"][:300],
        uid=str(payload["uid"])[:100],
        classname=display("classname"),
        ratio=display("ratio"),
        uploaded=display("uploaded"),
        downloaded=display("downloaded"),
        seedbonus=points if points is not None and math.isfinite(points) and points >= 0 else None,
        vip_until=vip_until_from(payload),
    )


def upload_wire_amount(amount):
    # MAM requires the trailing space. The public Dewarr API uses a simpler enum.
    return "Max Affordable " if amount == "max" else amount
