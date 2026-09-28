"""Compatibility for retired request quotas; saved policies never restrict acquisition.

Keep response shapes and historical rows readable for older clients. Request
admission, pending requests and transfer bytes are unlimited for every user.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import exists, func, select

from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionTarget,
    RequestQuotaCharge,
)

LOCK = "request-quotas:admission"


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    medium: Literal["ebook", "audio", "combined"] = "combined"
    window: Literal["day", "week", "month"] = "week"
    books: int | None = Field(default=None, ge=0, le=1000000)
    size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    windows: list[Window] = Field(default_factory=list, max_length=9)
    pending_cap: int | None = Field(default=None, ge=0, le=1000000)
    exempt_admin_approved: bool = False

    @model_validator(mode="after")
    def unique_windows(self):
        keys = [(item.medium, item.window) for item in self.windows]
        if len(keys) != len(set(keys)):
            raise ValueError("Choose each medium/window combination only once")
        return self


class WindowUsage(Window):
    used_books: int
    used_bytes: int
    remaining_books: int | None
    remaining_bytes: int | None
    capacity_returns_at: datetime | None = None


class Usage(BaseModel):
    user_id: str
    user_name: str
    source: str
    bypass: bool
    pending: int
    pending_remaining: int | None
    rules: Rules
    windows: list[WindowUsage]


class QuotaExceeded(HTTPException):
    def __init__(self, message, retry_at=None):
        self.retry_at = retry_at
        super().__init__(
            429,
            message,
            headers={
                "Retry-After": str(max(1, int((retry_at - datetime.now(UTC)).total_seconds())))
            }
            if retry_at
            else None,
        )


async def effective(db, user):
    return Rules(), "installation"


async def pending_count(db, owner_id, *, excluding=None):
    pending = exists(
        select(AcquisitionReason.id).where(
            AcquisitionReason.intent_id == AcquisitionIntent.id,
            AcquisitionReason.active.is_(True),
            AcquisitionReason.approval_status == "pending",
        )
    )
    approved = exists(
        select(AcquisitionReason.id).where(
            AcquisitionReason.intent_id == AcquisitionIntent.id,
            AcquisitionReason.active.is_(True),
            AcquisitionReason.approval_status == "approved",
        )
    )
    admitted = exists(
        select(RequestQuotaCharge.target_id)
        .join(AcquisitionTarget)
        .where(
            AcquisitionTarget.intent_id == AcquisitionIntent.id,
            RequestQuotaCharge.exempt.is_(False),
        )
    )
    query = (
        select(func.count())
        .select_from(AcquisitionIntent)
        .where(
            AcquisitionIntent.owner_id == owner_id,
            pending,
            ~approved,
            admitted,
        )
    )
    if excluding:
        query = query.where(AcquisitionIntent.id != excluding)
    return await db.scalar(query) or 0


async def admin_exempt(db, intent, rules):
    return False


async def admit(db, user, intent, target, medium, *, pending=False, shared=False):
    # No new quota ledger entries: history is retained solely for compatibility.
    return


async def reserve_size(db, user, target, medium, size_bytes):
    return


async def usage(db, user):
    return Usage(
        user_id=str(user.id),
        user_name=user.display_name,
        source="installation",
        bypass=True,
        pending=await pending_count(db, user.id),
        pending_remaining=None,
        rules=Rules(),
        windows=[],
    )


async def hold_selection(db, operation, error):
    return
