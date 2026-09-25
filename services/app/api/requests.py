import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.api.dependencies import CurrentUser, Database, Member
from app.api.operations import OperationView
from app.api.quick_add_feedback import QuickAddView, quick_add_view
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionSelection,
    AcquisitionTarget,
    AssetContains,
    AuditEvent,
    AutomaticImportContinuation,
    BookList,
    DownloadAttempt,
    DownloadHandoff,
    DownloadMembership,
    DownloadRecovery,
    DownloadRepair,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    Operation,
    User,
    Version,
    Work,
)
from app.domain.acquisition import (
    RequestOptions,
    RequestReason,
    RequestSpec,
    assess,
    evaluate,
    submit,
    validate_request,
)
from app.domain.availability import owned_coverage
from app.domain.display_requests import ExistingCopyHint, existing_copy_hints
from app.domain.list_series import SeriesPlanView
from app.domain.permissions import (
    AUTO_APPROVE,
    AUTO_APPROVE_AUDIO,
    AUTO_APPROVE_EBOOK,
    MANAGE_REQUESTS,
    auto_approves,
    effective_permissions,
    has,
)
from app.domain.release_profiles import ProfileSnapshot
from app.domain.request_approvals import approval_download_started
from app.domain.request_preferences import PreferenceChoice, resolve
from app.domain.work_graph import acquisition_lock, canonical_map, canonical_work, family_ids

router = APIRouter(prefix="/requests", tags=["requests"])
logger = logging.getLogger(__name__)
# One filter page projects at most this many candidates. The rest resume from next_offset.
_FILTER_SCAN_LIMIT = 40


class RequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_id: UUID
    specification: RequestOptions
    reason: RequestReason = Field(default_factory=RequestReason)
    release_preferences: PreferenceChoice | None = None
    expected_preference_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class TargetView(BaseModel):
    slot: str
    state: str
    message: str
    source_artifact_id: UUID | None = None
    next_action: Literal["none", "search", "selected-release", "downloads", "book"] = "none"
    progress: float | None = None
    selection_status: str | None = None
    attempt_state: str | None = None
    attempt_id: UUID | None = None
    can_view_download_history: bool = False
    can_cancel: bool = False
    can_recheck: bool = False
    needs_review: bool = False
    shared_download: bool = False
    can_claim: bool = False
    review_revision: str | None = None
    inspection_id: UUID | None = None
    review_retry: bool = False
    review_reassignment: bool = False
    review_message: str | None = None
    attempt_message: str | None = None
    repair_message: str | None = None
    can_repair: bool = False
    shared_books: list[str] = Field(default_factory=list)
    transfer_notes: list[str] = Field(default_factory=list)


class ReasonView(BaseModel):
    label: str
    id: UUID
    kind: str
    active: bool
    list_id: UUID | None
    approval_status: str = "approved"
    decision_note: str | None = None
    release_policy: ProfileSnapshot | None = None


class RequestView(BaseModel):
    id: UUID
    work_id: UUID
    work_title: str
    owner_name: str = ""
    can_open_book: bool = False
    can_decide: bool = False
    can_start_download: bool = False
    can_withdraw: bool = False
    approval_status: str = "approved"
    cover_url: str | None = None
    authors: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    specification: RequestSpec
    targets: list[TargetView]
    reasons: list[ReasonView]
    description: str
    release_policy: ProfileSnapshot | None = None


class PreviewView(BaseModel):
    existing_copies: list[ExistingCopyHint] = Field(default_factory=list)
    specification: RequestSpec
    targets: list[TargetView]
    download_available: bool = False
    release_policy: ProfileSnapshot | None = None
    series_scope: SeriesPlanView | None = None


class SubmittedView(BaseModel):
    request: RequestView
    operation: OperationView


class RequestPage(BaseModel):
    items: list[RequestView]
    total: int
    offset: int
    limit: int
    next_offset: int | None = None
    total_bounded: bool = False


async def owned_intent(db, user, intent_id):
    intent = await db.scalar(
        select(AcquisitionIntent).where(
            AcquisitionIntent.id == intent_id,
            AcquisitionIntent.owner_id == user.id,
        )
    )
    if not intent:
        raise HTTPException(404, "Request not found")
    return intent


async def readable_intent(db, user, intent_id):
    intent = await db.get(AcquisitionIntent, intent_id)
    if intent and (intent.owner_id == user.id or has(user, MANAGE_REQUESTS)):
        return intent
    raise HTTPException(404, "Request not found")


def _download_modes(user) -> list[str] | None:
    """None covers every medium. An empty list cannot start an approval download."""
    if not has(user, MANAGE_REQUESTS):
        return []
    perms = effective_permissions(user)
    if user.role == "admin" or perms & AUTO_APPROVE:
        return None
    ebook = bool(perms & AUTO_APPROVE_EBOOK)
    audio = bool(perms & AUTO_APPROVE_AUDIO)
    if ebook and audio:
        return None
    if ebook:
        return ["ebook"]
    if audio:
        return ["audio"]
    return []


def _ready_for_download():
    mode = func.coalesce(AcquisitionIntent.specification["mode"].astext, "")
    perms = User.permissions
    umbrella = perms.bitwise_and(AUTO_APPROVE) != 0
    ebook = perms.bitwise_and(AUTO_APPROVE_EBOOK) != 0
    audio = perms.bitwise_and(AUTO_APPROVE_AUDIO) != 0
    specific = or_(
        and_(mode == "ebook", ebook),
        and_(mode == "audio", audio),
        and_(mode.not_in(["ebook", "audio"]), ebook, audio),
    )
    owner_can = and_(
        User.active.is_(True),
        or_(
            User.role == "admin",
            and_(User.role != "viewer", User.permissions.is_(None)),
            and_(User.role != "viewer", or_(umbrella, specific)),
        ),
    )
    return [
        exists(
            select(AcquisitionReason.id).where(
                AcquisitionReason.intent_id == AcquisitionIntent.id,
                AcquisitionReason.active.is_(True),
                AcquisitionReason.approval_status == "approved",
            )
        ),
        ~exists(
            select(AcquisitionReason.id).where(
                AcquisitionReason.intent_id == AcquisitionIntent.id,
                AcquisitionReason.active.is_(True),
                AcquisitionReason.approval_status == "pending",
            )
        ),
        ~exists(select(User.id).where(User.id == AcquisitionIntent.owner_id, owner_can)),
        ~exists(
            select(Operation.id).where(
                Operation.kind == "acquisition.quick-add",
                Operation.status.in_(["queued", "running", "completed"]),
                Operation.payload["approval_dispatch"].astext == "true",
                Operation.payload["command"]["work_id"].astext
                == cast(AcquisitionIntent.work_id, String),
            )
        ),
    ]


def approval_of(reasons) -> str:
    active = [reason for reason in reasons if reason.active]
    if any(reason.approval_status == "pending" for reason in active):
        return "pending"
    if any(reason.approval_status == "approved" for reason in active):
        return "approved"
    if any(reason.approval_status == "declined" for reason in active):
        return "declined"
    return "approved"


_LIVE_DOWNLOADS = ("queued", "preflight", "submitting", "uncertain", "downloading", "held")
RequestStatus = Literal["pending", "downloading", "library", "declined", "withdrawn", "review"]


def _active_reasons(*extra):
    return select(AcquisitionReason.id).where(
        AcquisitionReason.intent_id == AcquisitionIntent.id,
        AcquisitionReason.active.is_(True),
        *extra,
    )


def _review_clause():
    """Completed transfers still waiting for an administrator import review."""
    active_handoff = exists().where(
        DownloadHandoff.attempt_id == DownloadAttempt.id,
        DownloadHandoff.active.is_(True),
    )
    committed_member = (
        select(DownloadMembership.selection_id)
        .join(AcquisitionSelection, AcquisitionSelection.id == DownloadMembership.selection_id)
        .where(
            DownloadMembership.attempt_id == DownloadAttempt.id,
            AcquisitionSelection.state == "committed",
        )
        .correlate(DownloadAttempt)
        .exists()
    )
    return and_(
        DownloadAttempt.state == "complete",
        or_(and_(DownloadAttempt.inspection_id.is_(None), committed_member), active_handoff),
    )


def _attempt_exists(*extra, wanted_target: bool = False):
    """True when this request's selection is the transfer or one of its members.

    wanted_target keeps the match on that selection's own target. A finished ebook
    then stays out of Downloading while a different format is still wanted.
    """

    def linked(statement):
        conditions = [AcquisitionSelection.intent_id == AcquisitionIntent.id, *extra]
        if wanted_target:
            statement = statement.join(
                AcquisitionTarget, AcquisitionTarget.id == AcquisitionSelection.target_id
            )
            conditions.append(AcquisitionTarget.state == "wanted")
        return exists(statement.where(*conditions))

    member = linked(
        select(DownloadAttempt.id)
        .join(DownloadMembership, DownloadMembership.attempt_id == DownloadAttempt.id)
        .join(AcquisitionSelection, AcquisitionSelection.id == DownloadMembership.selection_id)
    )
    direct = linked(
        select(DownloadAttempt.id).join(
            AcquisitionSelection, AcquisitionSelection.id == DownloadAttempt.selection_id
        )
    )
    return or_(member, direct)


def _status_filters(status: RequestStatus):
    if status == "pending":
        return [exists(_active_reasons(AcquisitionReason.approval_status == "pending"))]
    if status == "declined":
        return [
            exists(_active_reasons(AcquisitionReason.approval_status == "declined")),
            ~exists(_active_reasons(AcquisitionReason.approval_status == "pending")),
            ~exists(_active_reasons(AcquisitionReason.approval_status == "approved")),
        ]
    if status == "withdrawn":
        return [~exists(_active_reasons())]
    if status == "library":
        return [_library_candidate()]
    if status == "downloading":
        return [
            or_(
                _attempt_exists(DownloadAttempt.state.in_(_LIVE_DOWNLOADS)),
                _attempt_exists(DownloadAttempt.state == "complete", wanted_target=True),
                _committed_selection(),
            )
        ]
    return [_attempt_exists(_review_clause())]


def _committed_selection():
    """A committed release is Downloading on the card before any transfer exists."""
    return exists(
        select(AcquisitionSelection.id).where(
            AcquisitionSelection.intent_id == AcquisitionIntent.id,
            AcquisitionSelection.state == "committed",
        )
    )


def _stored_satisfied():
    return exists(
        select(AcquisitionTarget.id).where(
            AcquisitionTarget.intent_id == AcquisitionIntent.id,
            AcquisitionTarget.state == "satisfied",
        )
    )


def _owner_visible_library():
    """A present copy the request owner can see. Rules are applied after this candidate set."""
    return exists(
        select(LibraryAsset.id)
        .join(Library, Library.id == LibraryAsset.library_id)
        .join(Integration, Integration.id == Library.integration_id)
        .join(AssetContains, AssetContains.asset_id == LibraryAsset.id)
        .join(User, User.id == AcquisitionIntent.owner_id)
        .where(
            AssetContains.work_id.in_(family_ids(AcquisitionIntent.work_id)),
            LibraryAsset.state == "present",
            owned_coverage(),
            Library.accessible.is_(True),
            Integration.enabled.is_(True),
            or_(
                User.role == "admin",
                exists(
                    select(LibraryGrant.library_id).where(
                        LibraryGrant.library_id == Library.id,
                        LibraryGrant.user_id == User.id,
                    )
                ),
            ),
        )
    )


def _library_candidate():
    return or_(_stored_satisfied(), _owner_visible_library())


def _title_sort():
    """Sort by the canonical title the card shows, then the original work title."""
    mapping = canonical_map()
    canonical = aliased(Work)
    origin = aliased(Work)
    canonical_title = (
        select(canonical.title)
        .join(mapping, mapping.c.work_id == canonical.id)
        .where(mapping.c.origin_id == AcquisitionIntent.work_id)
        .limit(1)
        .scalar_subquery()
    )
    origin_title = (
        select(origin.title).where(origin.id == AcquisitionIntent.work_id).scalar_subquery()
    )
    return func.coalesce(canonical_title, origin_title)


def _chip(card, target) -> str:
    """Same order as the request card status label."""
    if not any(reason.active for reason in card.reasons):
        return "withdrawn"
    if card.approval_status == "declined" or target.message == "Request declined":
        return "declined"
    if card.approval_status == "pending" or target.message == "Waiting for approval":
        return "pending"
    if target.attempt_state in _LIVE_DOWNLOADS:
        return "downloading"
    if target.state == "awaiting-inventory":
        return "check-inventory"
    if target.state == "paused":
        return "paused"
    if target.attempt_state == "complete" and target.state != "satisfied":
        return "importing"
    if target.state == "satisfied":
        return "in-library"
    if target.next_action == "downloads" and target.attempt_state != "cancelled":
        return "downloading"
    if getattr(target, "selection_status", None) in {"held", "failed"}:
        return "download-not-started"
    if getattr(target, "selection_status", None) in {"queued", "running"}:
        return "preparing-download"
    if target.state == "wanted":
        return "wanted"
    if target.state == "cancelled":
        return "withdrawn"
    return target.state


def _matches_card(card, status: str) -> bool:
    chips = [_chip(card, target) for target in card.targets]
    if status == "library":
        return "in-library" in chips
    if status == "downloading":
        return "downloading" in chips or "importing" in chips
    return True


async def _decorate_target(db, user, intent, target: TargetView) -> None:
    row = await db.scalar(
        select(AcquisitionTarget).where(
            AcquisitionTarget.intent_id == intent.id,
            AcquisitionTarget.slot == target.slot,
        )
    )
    if not row:
        return
    selection_id = await db.scalar(
        select(AcquisitionSelection.id)
        .where(
            AcquisitionSelection.target_id == row.id,
            or_(
                AcquisitionSelection.state.in_(["prepared", "committed", "fulfilled"]),
                exists().where(DownloadRecovery.selection_id == AcquisitionSelection.id),
            ),
        )
        .order_by(AcquisitionSelection.created_at.desc())
        .limit(1)
    )
    if not selection_id:
        return
    attempt = await db.scalar(
        select(DownloadAttempt)
        .join(DownloadMembership, DownloadMembership.attempt_id == DownloadAttempt.id)
        .where(DownloadMembership.selection_id == selection_id)
        .order_by(DownloadAttempt.created_at.desc())
        .limit(1)
    )
    if not attempt:
        attempt = await db.scalar(
            select(DownloadAttempt)
            .where(DownloadAttempt.selection_id == selection_id)
            .order_by(DownloadAttempt.created_at.desc())
            .limit(1)
        )
    if not attempt:
        return
    raw = (attempt.observation or {}).get("progress")
    now = datetime.now(UTC)
    repair = await db.scalar(
        select(DownloadRepair)
        .where(DownloadRepair.attempt_id == attempt.id, DownloadRepair.state == "pending")
        .limit(1)
    )
    members = list(
        await db.scalars(
            select(AcquisitionSelection)
            .join(DownloadMembership, DownloadMembership.selection_id == AcquisitionSelection.id)
            .where(DownloadMembership.attempt_id == attempt.id)
            .order_by(AcquisitionSelection.id)
        )
    )
    target.attempt_id = attempt.id
    target.attempt_state = attempt.state
    target.progress = (
        float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None
    )
    target.shared_download = len(members) > 1
    target.shared_books = [
        label for item in members if item.id != selection_id and (label := _shared_book(item))
    ]
    if attempt.state in {"held", "uncertain"} and attempt.message:
        target.attempt_message = attempt.message
    if repair:
        target.repair_message = repair.message
    target.transfer_notes = [
        (
            "Additional books need attention: " + item.message
            if item.state == "held"
            else "Additional books: " + item.message
        )
        for item in await db.scalars(
            select(AutomaticImportContinuation)
            .where(
                AutomaticImportContinuation.attempt_id == attempt.id,
                AutomaticImportContinuation.state != "complete",
            )
            .order_by(AutomaticImportContinuation.created_at, AutomaticImportContinuation.id)
        )
    ]
    owns = attempt.owner_id == user.id
    target.can_view_download_history = owns
    recovering = await db.scalar(
        select(DownloadRecovery.id).where(DownloadRecovery.attempt_id == attempt.id).limit(1)
    )
    target.can_cancel = owns and not attempt.external_may_exist and attempt.state != "cancelled"
    target.can_recheck = (
        owns
        and not recovering
        and attempt.state != "cancelled"
        and not repair
        and (not attempt.lease_until or attempt.lease_until <= now)
        and (not attempt.next_check_at or attempt.next_check_at <= now)
    )
    target.can_repair = not recovering and await _can_repair(db, user, attempt, repair, now)
    if user.role != "admin" or attempt.state != "complete":
        return
    queued = await db.scalar(
        select(DownloadAttempt.id).where(DownloadAttempt.id == attempt.id, _review_clause())
    )
    if not queued:
        return
    from app.domain.download_reviews import queue_view

    selection = await db.get(AcquisitionSelection, attempt.selection_id)
    if not selection:
        return
    try:
        review = await queue_view(db, user, attempt, selection)
    except (HTTPException, AttributeError, KeyError, TypeError, ValueError):
        logger.exception("Skipped review details for attempt %s", attempt.id)
        target.needs_review = True
        target.review_message = "Review details need attention"
        return
    target.needs_review = True
    target.can_claim = bool(review["can_claim"])
    target.review_message = review["message"]
    target.review_revision = review["revision"]
    target.inspection_id = review["inspection_id"]
    target.review_retry = bool(review["retry"])
    target.review_reassignment = bool(review["reassignment"])


def _shared_book(selection) -> str | None:
    frozen = selection.frozen or {}
    title = frozen.get("work_title")
    if not isinstance(title, str) or not title:
        return None
    medium = (frozen.get("requirements") or {}).get("medium")
    return title + " · " + ("Audiobook" if medium == "audio" else "Ebook")


async def _can_repair(db, user, attempt, repair, now) -> bool:
    if (
        user.role != "admin"
        or repair
        or not attempt.external_may_exist
        or attempt.state in {"complete", "cancelled"}
        or (attempt.lease_until and attempt.lease_until > now)
    ):
        return False
    selection = await db.get(AcquisitionSelection, attempt.selection_id)
    if not selection:
        return False
    try:
        from app.domain.acquisition_selection import configuration_current
        from app.domain.download_repairs import accepted_configuration

        return not await configuration_current(
            db,
            selection,
            committed=True,
            configuration=await accepted_configuration(db, selection),
        )
    except (HTTPException, AttributeError, KeyError, TypeError):
        return False


def projection_page(matches: list[bool], *, cursor: int, limit: int, budget: int):
    """Indexes that fit on one filter page, and the candidate cursor for the next page.

    The cursor counts candidate rows, including ones whose chip does not match. Scanning
    stops once the page is full or the budget is spent, so a long history is not projected
    just to count it.
    """
    kept: list[int] = []
    index = cursor
    examined = 0
    while index < len(matches) and len(kept) < limit and examined < budget:
        if matches[index]:
            kept.append(index)
        index += 1
        examined += 1
    return kept, index if index < len(matches) else None


async def _projected_page(db, user, listing, counted, order, status, offset, limit):
    """Keep a filter page on the chip, and resume later instead of projecting every row."""
    cursor = offset
    kept: list[RequestView] = []
    exhausted = False
    while len(kept) < limit and cursor - offset < _FILTER_SCAN_LIMIT:
        remaining = _FILTER_SCAN_LIMIT - (cursor - offset)
        batch_limit = min(20, remaining)
        rows = (await db.scalars(listing.order_by(*order).offset(cursor).limit(batch_limit))).all()
        if not rows:
            exhausted = True
            break
        flags: list[bool] = []
        cards: list[RequestView | None] = []
        for intent in rows:
            try:
                card = await view(db, user, intent)
            except Exception:
                logger.exception("Skipped request %s while filtering", intent.id)
                card = None
            matched = card is not None and _matches_card(card, status)
            flags.append(matched)
            cards.append(card if matched else None)
        indexes, next_in_batch = projection_page(
            flags, cursor=0, limit=limit - len(kept), budget=remaining
        )
        for index in indexes:
            card = cards[index]
            if card is not None:
                kept.append(card)
        if next_in_batch is None:
            cursor += len(rows)
            if len(rows) < batch_limit:
                exhausted = True
                break
            continue
        cursor += next_in_batch
        break
    next_offset = None if exhausted or cursor <= offset else cursor
    exact = exhausted and offset == 0
    total = len(kept) if exact else await db.scalar(counted) or 0
    return RequestPage(
        items=kept,
        total=total,
        offset=offset,
        limit=limit,
        next_offset=next_offset,
        total_bounded=not exact,
    )


async def view(db, user, intent):
    # Refresh only the display projection here; dispatch must evaluate under the work lock.
    spec = RequestSpec.model_validate(intent.specification)
    reasons = (
        await db.scalars(
            select(AcquisitionReason)
            .where(
                AcquisitionReason.intent_id == intent.id,
            )
            .order_by(AcquisitionReason.created_at, AcquisitionReason.id)
        )
    ).all()
    active = any(reason.active and reason.approval_status == "approved" for reason in reasons)
    pending = any(reason.active and reason.approval_status == "pending" for reason in reasons)
    declined = any(reason.active and reason.approval_status == "declined" for reason in reasons)
    approval = approval_of(reasons)
    owner = await db.get(User, intent.owner_id)
    can_dispatch = has(user, MANAGE_REQUESTS) and auto_approves(user, spec)
    download_followup = (
        can_dispatch
        and approval == "approved"
        and not (owner and auto_approves(owner, spec))
        and not await approval_download_started(db, intent.work_id)
    )
    can_start_download = (can_dispatch and approval == "pending") or download_followup
    descriptions = []
    work_title = "Unavailable book"
    can_open_book = False
    try:
        if user.role == "viewer":
            raise HTTPException(403, "Read-only account")
        work_title = (
            await validate_request(db, user, intent.work_id, spec, check_version_constraints=False)
        ).title
        can_open_book = True
        for medium in ("ebook", "audio"):
            version_id = getattr(spec, medium + "_version_id")
            if version_id:
                version = await db.get(Version, version_id)
                if not version:
                    descriptions.append("Selected version")
                    continue
                descriptors = [
                    version.title,
                    ", ".join(version.narrators or []),
                    str(version.publication_year) if version.publication_year else None,
                ]
                descriptions.append(
                    " · ".join(value for value in descriptors if value) or "Selected version"
                )
        # Status follows the requester's libraries. An approver can see other
        # libraries, and those copies do not satisfy someone else's request.
        targets = [
            TargetView(**item) for item in await assess(db, owner or user, intent.work_id, spec)
        ]
        for target in targets:
            if not active:
                if pending:
                    target.state, target.message = "paused", "Waiting for approval"
                elif declined:
                    target.state, target.message = "cancelled", "Request declined"
                else:
                    target.state, target.message = "cancelled", "No active request reasons"
            elif target.state == "wanted" and not (
                intent.owner_id == user.id and auto_approves(user, spec)
            ):
                target.next_action = "none"
                target.message = (
                    "Approved. Start the download from this review."
                    if download_followup
                    else "Approved. An account that can download will add it to the library."
                )
            elif target.state == "wanted":
                target.next_action = "search"
                target.message = "Saved to wanted; choose a source release to continue"
                selection = await db.scalar(
                    select(AcquisitionSelection)
                    .join(
                        AcquisitionTarget,
                        AcquisitionTarget.reservation_id == AcquisitionSelection.reservation_id,
                    )
                    .where(
                        AcquisitionTarget.intent_id == intent.id,
                        AcquisitionTarget.slot == target.slot,
                        AcquisitionSelection.state.in_(["prepared", "committed"]),
                    )
                )
                if selection:
                    target.next_action = "none"
                    target.message = (
                        "Acquisition pending; check download activity"
                        if selection.state == "committed"
                        else "Release selected; download has not started"
                    )
                    if selection.owner_id == user.id:
                        target.source_artifact_id = selection.artifact_id
                        target.next_action = (
                            "downloads" if selection.state == "committed" else "selected-release"
                        )
                else:
                    quick = await db.scalar(
                        select(Operation)
                        .where(
                            Operation.owner_id == intent.owner_id,
                            Operation.kind == "acquisition.quick-add",
                            Operation.payload["intent_id"].astext == str(intent.id),
                        )
                        .order_by(Operation.created_at.desc())
                        .limit(1)
                    )
                    if quick and quick.status in {"queued", "running", "held"}:
                        target.message = quick.message
                    selected = await db.scalar(
                        select(Operation)
                        .where(
                            Operation.owner_id == intent.owner_id,
                            Operation.kind == "acquisition.auto-select",
                            Operation.payload["command"]["intent_id"].astext == str(intent.id),
                            Operation.payload["command"]["slot"].astext == target.slot,
                        )
                        .order_by(Operation.created_at.desc(), Operation.id.desc())
                        .limit(1)
                    )
                    if selected and (not quick or selected.created_at >= quick.created_at):
                        from app.domain.release_download_status import selection_feedback

                        target.selection_status = selected.status
                        target.message = selection_feedback(selected)[0]
                        if selected.status in {"queued", "running"}:
                            target.next_action = "none"
            else:
                target.next_action = "book"
        if owner and owner.id != user.id:
            for target in targets:
                if target.message == "Already available in your library":
                    target.message = "Already in the requester's library"
    except HTTPException:
        can_open_book = False
        targets = [
            TargetView(slot=slot, state="paused", message="Request access needs attention")
            for slot in spec.slots()
        ]
    saved_targets = {
        row.slot: row
        for row in await db.scalars(
            select(AcquisitionTarget).where(AcquisitionTarget.intent_id == intent.id)
        )
    }
    for target in targets:
        saved = saved_targets.get(target.slot)
        if saved and saved.quota_waiting and target.state != "satisfied":
            target.state, target.message, target.next_action = "paused", saved.message, "none"
        await _decorate_target(db, user, intent, target)
    work_id = intent.work_id
    cover_url = None
    authors: list[str] = []
    try:
        work = await canonical_work(db, intent.work_id)
    except HTTPException:
        work = None
    if work is not None:
        work_id = work.id
        if can_open_book:
            cover_url = work.cover_url
            authors = [author for author in (work.authors or []) if isinstance(author, str)]
    return RequestView(
        id=intent.id,
        work_id=work_id,
        work_title=work_title,
        owner_name=owner.display_name if owner else "",
        can_open_book=can_open_book,
        can_decide=has(user, MANAGE_REQUESTS) and approval == "pending",
        can_start_download=can_start_download,
        can_withdraw=intent.owner_id == user.id,
        approval_status=approval,
        cover_url=cover_url,
        authors=authors,
        created_at=intent.created_at,
        specification=spec,
        release_policy=intent.release_policy,
        description="; ".join(
            descriptions
            + (["Language: " + spec.language] if spec.language else [])
            + (["Standalone copy"] if spec.standalone else [])
        )
        or "Any acceptable version",
        targets=targets,
        reasons=[
            ReasonView(
                id=reason.id,
                kind=reason.kind,
                active=reason.active,
                list_id=reason.list_id,
                approval_status=reason.approval_status,
                decision_note=reason.decision_note,
                release_policy=reason.release_policy,
                label=await _reason_label(db, user, intent, owner, reason),
            )
            for reason in reasons
        ],
    )


async def _reason_label(db, user, intent, owner, reason) -> str:
    if reason.kind == "series":
        return await _series_label(db, reason)
    if reason.kind == "manual" and intent.owner_id == user.id:
        return "Your request"
    if reason.kind == "manual":
        return (owner.display_name if owner else "Someone") + " requested this"
    name = await db.scalar(select(BookList.name).where(BookList.id == reason.list_id))
    return name or "Former list"


async def _series_label(db, reason) -> str:
    try:
        operation = await db.get(Operation, UUID(str(reason.reference)))
        name = operation.payload["series"]["name"] if operation and operation.payload else None
    except (AttributeError, KeyError, TypeError, ValueError):
        name = None
    if not isinstance(name, str) or not name.strip():
        return "Series"
    return "Series: " + name


class QuickAddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_id: UUID
    specification: RequestOptions = Field(default_factory=RequestOptions)


@router.post("/quick-add", response_model=QuickAddView, status_code=202)
async def quick_add(
    body: QuickAddInput,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=160),
):
    from app.domain.quick_add import begin

    operation = await begin(db, user, body.work_id, body.specification, idempotency_key)
    await db.flush()
    await db.refresh(operation)
    response = await quick_add_view(db, operation)
    await db.commit()
    return response


@router.get("/quick-add/latest/{work_id}", response_model=QuickAddView | None)
async def latest_quick_add(work_id: UUID, user: Member, db: Database):
    from app.domain.quick_add import KIND, repair

    operation = await db.scalar(
        select(Operation)
        .where(
            Operation.owner_id == user.id,
            Operation.kind == KIND,
            Operation.payload["command"]["work_id"].astext == str(work_id),
        )
        .order_by(Operation.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if operation:
        await repair(db, operation)
        await db.commit()
        await db.refresh(operation)
        return await quick_add_view(db, operation)
    return None


@router.post("/preview", response_model=PreviewView)
async def preview(body: RequestInput, user: CurrentUser, db: Database):
    specification, profile = await resolve(
        db, user, body.specification, body.reason, body.release_preferences
    )
    await validate_request(db, user, body.work_id, specification, body.reason)
    expansion = None
    if profile.preferences.effective_series_scope == "complete_series":
        from app.domain.list_series import plan

        expansion = await plan(db, user, (await canonical_work(db, body.work_id)).id)
    return PreviewView(
        existing_copies=await existing_copy_hints(db, user, body.work_id, specification),
        specification=specification,
        release_policy=profile,
        series_scope=expansion,
        targets=[
            TargetView(**item)
            for item in await assess(
                db,
                user,
                body.work_id,
                specification,
            )
        ],
    )


@router.post("", response_model=SubmittedView, status_code=202)
async def create(
    body: RequestInput,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    intent, operation = await submit(
        db,
        user,
        body.work_id,
        body.specification,
        body.reason,
        idempotency_key,
        preference_choice=body.release_preferences,
        expected_preference_revision=body.expected_preference_revision,
    )
    response = SubmittedView(
        request=await view(db, user, intent), operation=OperationView.model_validate(operation)
    )
    await db.commit()
    return response


@router.get("", response_model=RequestPage)
async def all_requests(
    user: CurrentUser,
    db: Database,
    work_id: UUID | None = None,
    active_only: bool = False,
    pending_only: bool = False,
    download_ready: bool = False,
    mine: bool = False,
    status: RequestStatus | None = None,
    sort: Literal["newest", "title"] = "newest",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    if pending_only and download_ready:
        raise HTTPException(422, "Choose either waiting requests or downloads")
    if status and (pending_only or download_ready):
        raise HTTPException(422, "Choose one request filter")
    if (pending_only or download_ready) and not has(user, MANAGE_REQUESTS):
        raise HTTPException(403, "You cannot review requests")
    if status == "review" and user.role != "admin":
        raise HTTPException(403, "You cannot review imports")
    if download_ready:
        modes = _download_modes(user)
        if modes == []:
            return RequestPage(items=[], total=0, offset=offset, limit=limit)
        where = _ready_for_download()
        if modes is not None:
            where.append(
                func.coalesce(AcquisitionIntent.specification["mode"].astext, "").in_(modes)
            )
    elif pending_only:
        where = [exists(_active_reasons(AcquisitionReason.approval_status == "pending"))]
    else:
        where = []
        if mine or not has(user, MANAGE_REQUESTS):
            where.append(AcquisitionIntent.owner_id == user.id)
        if status:
            where.extend(_status_filters(status))
    if active_only:
        where.append(exists(_active_reasons()))
    if work_id:
        where.append(AcquisitionIntent.work_id.in_(family_ids(work_id)))
    order = (
        [_title_sort().asc(), AcquisitionIntent.id]
        if sort == "title"
        else [AcquisitionIntent.created_at.desc(), AcquisitionIntent.id]
    )
    listing = select(AcquisitionIntent)
    counted = select(func.count()).select_from(AcquisitionIntent)
    if where:
        listing = listing.where(*where)
        counted = counted.where(*where)
    if status in {"library", "downloading"}:
        return await _projected_page(db, user, listing, counted, order, status, offset, limit)
    intents = (await db.scalars(listing.order_by(*order).offset(offset).limit(limit))).all()
    total = await db.scalar(counted)
    return RequestPage(
        items=[await view(db, user, intent) for intent in intents],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.get("/{intent_id}", response_model=RequestView)
async def request_detail(intent_id: UUID, user: CurrentUser, db: Database):
    return await view(db, user, await readable_intent(db, user, intent_id))


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["approved", "declined"]
    note: str | None = Field(default=None, max_length=300)
    download: bool = False
    expected_status: Literal["pending", "approved", "declined"]


class DecisionView(BaseModel):
    request: RequestView
    download_started: bool
    download_message: str | None = None


@router.post("/{intent_id}/decision", response_model=DecisionView)
async def decision(
    intent_id: UUID,
    body: DecisionInput,
    user: CurrentUser,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    from app.domain.request_approvals import decide

    intent, started, message = await decide(
        db,
        user,
        intent_id,
        body.status,
        body.note,
        body.download,
        body.expected_status,
        idempotency_key,
    )
    response = DecisionView(
        request=await view(db, user, intent),
        download_started=started,
        download_message=message,
    )
    await db.commit()
    return response


@router.delete("/{intent_id}/reasons/{reason_id}", response_model=RequestView)
async def cancel_reason(intent_id: UUID, reason_id: UUID, user: Member, db: Database):
    intent = await owned_intent(db, user, intent_id)
    await acquisition_lock(db, intent.work_id)
    reason = await db.scalar(
        select(AcquisitionReason)
        .where(
            AcquisitionReason.id == reason_id,
            AcquisitionReason.intent_id == intent_id,
        )
        .execution_options(populate_existing=True)
    )
    if not reason:
        raise HTTPException(404, "Request reason not found")
    reason.active = False
    await db.flush()
    await evaluate(db, user, intent)
    db.add(AuditEvent(actor_id=user.id, action="acquisition.reason.cancelled", entity_id=reason.id))
    response = await view(db, user, intent)
    await db.commit()
    return response
