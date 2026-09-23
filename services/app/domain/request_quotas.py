"""Rolling request admissions. Call under acquisition_lock, in the creating transaction.

Admissions survive cancellation, inventory refresh and failed-download replacement.
Size is reserved when a descriptor is selected, using its own rolling timestamp.
A month is a rolling 30 days, rather than a calendar-month reset.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import exists, func, or_, select

from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionTarget,
    RequestQuotaCharge,
    RequestQuotaPolicy,
    User,
)
from app.domain.permissions import BYPASS_QUOTAS, has

DAYS = {"day": 1, "week": 7, "month": 30}
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
    scopes = [f"user:{user.id}"]
    if user.permission_role_id:
        scopes.append(f"role:{user.permission_role_id}")
    scopes.extend([f"role:{user.role}", "installation"])
    rows = {
        row.scope: row
        for row in await db.scalars(
            select(RequestQuotaPolicy).where(RequestQuotaPolicy.scope.in_(scopes))
        )
    }
    for scope in scopes:
        if scope in rows:
            return Rules.model_validate(rows[scope].configuration), scope
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


async def charges(db, owner_id):
    return list(
        await db.scalars(
            select(RequestQuotaCharge).where(
                RequestQuotaCharge.owner_id == owner_id,
                RequestQuotaCharge.exempt.is_(False),
                or_(
                    RequestQuotaCharge.admitted_at > datetime.now(UTC) - timedelta(days=30),
                    RequestQuotaCharge.size_at > datetime.now(UTC) - timedelta(days=30),
                ),
            )
        )
    )


def window_usage(rule, rows, now):
    cutoff = now - timedelta(days=DAYS[rule.window])
    rows = [row for row in rows if rule.medium == "combined" or row.medium == rule.medium]
    counted = [row for row in rows if row.admitted_at > cutoff]
    sized = [row for row in rows if row.size_at and row.size_at > cutoff]
    books, size = len(counted), sum(row.size_bytes for row in sized)
    return counted, sized, books, size


def returns_at(events, used, amount, limit, duration):
    if amount > limit:
        return None
    for stamp, value in sorted(events):
        used -= value
        if used + amount <= limit:
            return stamp + duration
    return None


def check_windows(rules, rows, medium, now, *, books=1, size=0):
    blocked = []
    for rule in rules.windows:
        if rule.medium not in {"combined", medium}:
            continue
        counted, sized, used_books, used_size = window_usage(rule, rows, now)
        duration = timedelta(days=DAYS[rule.window])
        for amount, used, limit, events, label in (
            (books, used_books, rule.books, [(r.admitted_at, 1) for r in counted], "books"),
            (size, used_size, rule.size_bytes, [(r.size_at, r.size_bytes) for r in sized], "bytes"),
        ):
            if amount and limit is not None and used + amount > limit:
                retry = returns_at(events, used, amount, limit, duration)
                blocked.append(
                    (retry, f"{rule.medium} quota of {limit} {label} per rolling {rule.window}")
                )
    if blocked:
        retry = max(t for t, _ in blocked) if all(t for t, _ in blocked) else None
        message = "Request exceeds " + blocked[0][1]
        if len(blocked) > 1:
            message += f" and {len(blocked) - 1} other limits"
        message += ". "
        message += (
            f"Capacity returns at {retry.isoformat()}."
            if retry
            else "This request needs a smaller release or a quota change."
        )
        raise QuotaExceeded(message, retry)


async def admin_exempt(db, intent, rules):
    if not rules.exempt_admin_approved:
        return False
    return bool(
        await db.scalar(
            select(AcquisitionReason.id)
            .join(User, User.id == AcquisitionReason.decided_by)
            .where(
                AcquisitionReason.intent_id == intent.id,
                AcquisitionReason.active.is_(True),
                AcquisitionReason.approval_status == "approved",
                User.role == "admin",
                User.id != intent.owner_id,
            )
            .limit(1)
        )
    )


async def admit(db, user, intent, target, medium, *, pending=False, shared=False):
    rules, _ = await effective(db, user)
    charge = await db.get(RequestQuotaCharge, target.id)
    exempt = has(user, BYPASS_QUOTAS) or shared or await admin_exempt(db, intent, rules)
    if exempt:
        if charge:
            charge.exempt = True
        return
    if pending and rules.pending_cap is not None:
        if await pending_count(db, user.id, excluding=intent.id) >= rules.pending_cap:
            raise QuotaExceeded(
                "Pending approval cap reached. Capacity returns when an approver "
                "decides a request or you withdraw one."
            )
    if charge:
        return
    now = datetime.now(UTC)
    check_windows(rules, await charges(db, user.id), medium, now)
    db.add(
        RequestQuotaCharge(
            target_id=target.id,
            owner_id=user.id,
            medium=medium,
            admitted_at=now,
            size_bytes=0,
            exempt=False,
        )
    )
    await db.flush()


async def reserve_size(db, user, target, medium, size_bytes):
    """Descriptor size is known before dispatch. Replacements reuse the same admission."""
    rules, _ = await effective(db, user)
    charge = await db.get(RequestQuotaCharge, target.id)
    if has(user, BYPASS_QUOTAS) or not charge or charge.exempt:
        return
    if size_bytes <= 0 and any(
        rule.size_bytes is not None and rule.medium in {"combined", medium}
        for rule in rules.windows
    ):
        raise QuotaExceeded(
            "The release size is unknown. Choose a release with a verified size "
            "before using a size quota."
        )
    now = datetime.now(UTC)
    rows = await charges(db, user.id)
    # Re-evaluate Either under the actual selected medium; never consume two book slots.
    if charge.medium != medium:
        check_windows(rules, [row for row in rows if row.target_id != target.id], medium, now)
    # Replace the prior reservation rather than charging the same target twice.
    delta = max(0, size_bytes - charge.size_bytes)
    other = [row for row in rows if row.target_id != target.id]
    try:
        check_windows(rules, other, medium, now, books=0, size=max(size_bytes, charge.size_bytes))
    except QuotaExceeded as exc:
        exc.requirement = {"medium": medium, "size_bytes": size_bytes}
        raise
    if delta or charge.size_at is None:
        charge.size_bytes = max(size_bytes, charge.size_bytes)
        charge.size_at = now
    charge.medium = medium
    await db.flush()


async def usage(db, user):
    rules, source = await effective(db, user)
    rows, now = await charges(db, user.id), datetime.now(UTC)
    windows = []
    for rule in rules.windows:
        _, _, count, size = window_usage(rule, rows, now)
        retry = None
        try:
            check_windows(
                Rules(windows=[rule]),
                rows,
                rule.medium,
                now,
                size=1 if rule.size_bytes is not None else 0,
            )
        except QuotaExceeded as exc:
            retry = exc.retry_at
        windows.append(
            WindowUsage(
                **rule.model_dump(),
                used_books=count,
                used_bytes=size,
                remaining_books=max(0, rule.books - count) if rule.books is not None else None,
                remaining_bytes=max(0, rule.size_bytes - size)
                if rule.size_bytes is not None
                else None,
                capacity_returns_at=retry,
            )
        )
    pending = await pending_count(db, user.id)
    return Usage(
        user_id=str(user.id),
        user_name=user.display_name,
        source=source,
        bypass=has(user, BYPASS_QUOTAS),
        pending=pending,
        pending_remaining=max(0, rules.pending_cap - pending)
        if rules.pending_cap is not None
        else None,
        rules=rules,
        windows=windows,
    )


async def hold_selection(db, operation, error):
    """Persist an automatic size hold after the selection savepoint rolled back."""
    if not isinstance(error, QuotaExceeded):
        return
    command = operation.payload["command"]
    from uuid import UUID

    target = await db.scalar(
        select(AcquisitionTarget).where(
            AcquisitionTarget.intent_id == UUID(command["intent_id"]),
            AcquisitionTarget.slot == command["slot"],
        )
    )
    if target:
        target.quota_waiting, target.quota_retry_at = True, error.retry_at
        target.quota_requirement = getattr(error, "requirement", None)
        target.state, target.message = "paused", "Waiting for quota. " + error.detail
    operation.payload = {**operation.payload, "waiting_for_quota": True}
    operation.message = "Waiting for quota. " + error.detail
