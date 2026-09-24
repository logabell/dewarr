"""Per-user permissions modeled on Seerr: named grants, role presets, and overrides.

Administrator accounts always receive every grant. A null stored set means the
account still uses its legacy role preset, so existing members keep downloading.
"""

from contextvars import ContextVar
from datetime import UTC, datetime

from fastapi import HTTPException

ADMIN = 1 << 0
MANAGE_USERS = 1 << 1
MANAGE_SETTINGS = 1 << 2
MANAGE_REQUESTS = 1 << 3
REQUEST = 1 << 4
REQUEST_EBOOK = 1 << 5
REQUEST_AUDIO = 1 << 6
AUTO_APPROVE = 1 << 7
AUTO_APPROVE_EBOOK = 1 << 8
AUTO_APPROVE_AUDIO = 1 << 9
REQUEST_ADVANCED = 1 << 10
AUTOMATE = 1 << 11
BYPASS_QUOTAS = 1 << 12

# Set while an approver starts a download for someone else's request.
approval_dispatch = ContextVar("approval_dispatch", default=False)
# Set while someone who can auto-download starts that download themselves.
download_authorization = ContextVar("download_authorization", default=None)

CATALOG = (
    (
        "bypass_quotas",
        BYPASS_QUOTAS,
        "Bypass request quotas",
        "Requests",
        "Request without count, size, or pending approval limits.",
    ),
    (
        "admin",
        ADMIN,
        "Administrator",
        "Administration",
        "Manage the instance and every other permission.",
    ),
    (
        "manage_users",
        MANAGE_USERS,
        "Manage users",
        "Administration",
        "Create accounts and edit roles or permissions.",
    ),
    (
        "manage_settings",
        MANAGE_SETTINGS,
        "Manage settings",
        "Administration",
        "Included with administrator. Server settings stay on that role.",
    ),
    (
        "manage_requests",
        MANAGE_REQUESTS,
        "Manage requests",
        "Administration",
        "Approve or decline books other people ask for.",
    ),
    (
        "request",
        REQUEST,
        "Request",
        "Requests",
        "Ask for ebooks and audiobooks. A media box limits this to that medium.",
    ),
    (
        "request_ebook",
        REQUEST_EBOOK,
        "Request ebooks",
        "Requests",
        "Ask for ebook editions, with or without Request.",
    ),
    (
        "request_audio",
        REQUEST_AUDIO,
        "Request audiobooks",
        "Requests",
        "Ask for audiobook editions, with or without Request.",
    ),
    (
        "request_advanced",
        REQUEST_ADVANCED,
        "Advanced requests",
        "Requests",
        "Choose a specific edition, narrator, profile, or format limit.",
    ),
    (
        "auto_approve",
        AUTO_APPROVE,
        "Auto-download",
        "Downloads",
        "Download requested books without waiting for approval.",
    ),
    (
        "auto_approve_ebook",
        AUTO_APPROVE_EBOOK,
        "Auto-download ebooks",
        "Downloads",
        "Download ebook requests immediately.",
    ),
    (
        "auto_approve_audio",
        AUTO_APPROVE_AUDIO,
        "Auto-download audiobooks",
        "Downloads",
        "Download audiobook requests immediately.",
    ),
    (
        "automate",
        AUTOMATE,
        "Automate lists",
        "Downloads",
        "Run unattended list and series acquisition.",
    ),
)

BITS = {name: bit for name, bit, *_rest in CATALOG}
ALL = 0
for _name, bit, *_rest in CATALOG:
    ALL |= bit

MEMBER = (
    REQUEST
    | REQUEST_EBOOK
    | REQUEST_AUDIO
    | AUTO_APPROVE
    | AUTO_APPROVE_EBOOK
    | AUTO_APPROVE_AUDIO
    | REQUEST_ADVANCED
)
REQUESTER = REQUEST | REQUEST_EBOOK | REQUEST_AUDIO
APPROVER = MEMBER | MANAGE_REQUESTS

PRESETS = (
    ("admin", "Administrator", "Manage the instance, users, and downloads.", ALL),
    (
        "member",
        "Member",
        "Request books and download them without waiting for approval.",
        MEMBER,
    ),
    (
        "approver",
        "Approver",
        "Download books and approve or decline other people's requests.",
        APPROVER,
    ),
    (
        "requester",
        "Requester",
        "Ask for books. An approver decides before anything downloads.",
        REQUESTER,
    ),
    ("viewer", "Viewer", "Browse the library without requesting or downloading.", 0),
)


def names_from_bits(permissions: int) -> list[str]:
    return [name for name, bit, *_rest in CATALOG if permissions & bit]


def bits_from_names(names: list[str]) -> int:
    unknown = sorted(set(names) - BITS.keys())
    if unknown:
        raise HTTPException(422, "Unknown permission: " + ", ".join(unknown))
    value = 0
    for name in names:
        value |= BITS[name]
    return value


def preset_bits(role: str, *, automate: bool = False) -> int:
    if role == "admin":
        return ALL
    if role == "viewer":
        return 0
    if role == "requester":
        return REQUESTER
    if role == "approver":
        return APPROVER
    value = MEMBER
    if automate:
        value |= AUTOMATE
    return value


def effective_permissions(user) -> int:
    if not user or not user.active:
        return 0
    if user.role == "admin":
        return ALL
    if user.role == "viewer":
        return 0
    if user.permissions is None:
        return MEMBER | (AUTOMATE if user.can_automate else 0)
    return int(user.permissions) & ~ADMIN


def has(user, bit: int) -> bool:
    return bool(effective_permissions(user) & bit)


def automation_allowed(user) -> bool:
    if not user or not user.active or user.role == "viewer":
        return False
    if user.role == "admin":
        return True
    if user.permissions is None:
        return bool(user.can_automate)
    return bool(int(user.permissions) & AUTOMATE)


def access_label(user, role_name: str | None = None) -> str:
    if role_name:
        return role_name
    if user.role == "admin":
        return "Administrator"
    if user.role == "viewer":
        return "Viewer"
    stored = effective_permissions(user)
    automate = bool(stored & AUTOMATE)
    if stored == REQUESTER or stored == REQUESTER | AUTOMATE:
        return "Requester"
    if stored == (APPROVER | (AUTOMATE if automate else 0)):
        return "Approver"
    if stored == (MEMBER | (AUTOMATE if automate else 0)):
        return "Member"
    return "Custom"


def sync_user_permissions(user, permissions: int, role_id=None):
    permissions = int(permissions)
    if permissions & ADMIN:
        permissions = ALL
        user.role = "admin"
        user.can_automate = True
    elif permissions == 0:
        user.role = "viewer"
        user.can_automate = False
    else:
        user.role = "member"
        user.can_automate = bool(permissions & AUTOMATE)
        permissions &= ~ADMIN
    user.permissions = permissions
    user.permission_role_id = role_id


def media_of(spec) -> set[str]:
    if spec.mode == "ebook":
        return {"ebook"}
    if spec.mode == "audio":
        return {"audio"}
    return {"ebook", "audio"}


def _explicit_advanced(original, explicit, preference_choice) -> bool:
    if "ebook_version_id" in explicit and original.ebook_version_id:
        return True
    if "audio_version_id" in explicit and original.audio_version_id:
        return True
    if "required_narrators" in explicit and original.required_narrators:
        return True
    if "download_constraints" in explicit and original.download_constraints:
        return True
    if "abridged" in explicit and original.abridged is not None:
        return True
    if "standalone" in explicit and original.standalone:
        return True
    if not preference_choice:
        return False
    if preference_choice.profile_id:
        return True
    return bool(preference_choice.overrides.model_fields_set)


def request_medium_allowed(perms: int, medium: str) -> bool:
    """Request covers both media until a media box narrows it. A media box also works alone."""
    specific = REQUEST_EBOOK if medium == "ebook" else REQUEST_AUDIO
    other = REQUEST_AUDIO if medium == "ebook" else REQUEST_EBOOK
    if perms & specific:
        return True
    if not perms & REQUEST:
        return False
    return not bool(perms & other)


def assert_can_request(user, resolved, explicit, original, preference_choice):
    if not user or not user.active or user.role == "viewer":
        raise HTTPException(403, "Your account no longer has permission to create requests")
    perms = effective_permissions(user)
    needed = media_of(resolved)
    if not all(request_medium_allowed(perms, medium) for medium in needed):
        if not perms & (REQUEST | REQUEST_EBOOK | REQUEST_AUDIO):
            raise HTTPException(403, "Your account cannot request books")
        if "ebook" in needed and not request_medium_allowed(perms, "ebook"):
            raise HTTPException(403, "Your account cannot request ebooks")
        raise HTTPException(403, "Your account cannot request audiobooks")
    if _explicit_advanced(original, explicit, preference_choice) and not perms & REQUEST_ADVANCED:
        raise HTTPException(403, "Advanced request options require permission")


async def waiting_for_approval(db, request_id) -> bool:
    from sqlalchemy import select

    from app.db.models import AcquisitionReason

    reasons = (
        await db.scalars(
            select(AcquisitionReason).where(
                AcquisitionReason.intent_id == request_id,
                AcquisitionReason.active.is_(True),
            )
        )
    ).all()
    if any(reason.approval_status == "approved" for reason in reasons):
        return False
    return any(reason.approval_status == "pending" for reason in reasons)


def apply_approval_wait(targets) -> None:
    for target in targets:
        if target.get("state") != "satisfied":
            target["state"] = "paused"
            target["message"] = "Waiting for approval"


def list_batch_message(count: int, waiting: int) -> str:
    noun = "book" if count == 1 else "books"
    if count and waiting == count:
        if count == 1:
            return (
                "Saved 1 book and sent it for approval; "
                "downloads stay paused until the request is approved"
            )
        return (
            f"Saved {count} books and sent them for approval; "
            "downloads stay paused until a request is approved"
        )
    if waiting:
        pending = "1 is" if waiting == 1 else f"{waiting} are"
        return f"Saved wanted media for {count} {noun}; {pending} waiting for approval"
    return f"Saved wanted media for {count} {noun}; downloads have not been started"


def series_batch_message(count: int, waiting: int) -> str:
    if count and waiting == count:
        if count == 1:
            return "Saved a request for 1 book; it is waiting for approval"
        return f"Saved requests for {count} books; they are waiting for approval"
    if waiting:
        pending = "1 is" if waiting == 1 else f"{waiting} are"
        return f"Saved requests for {count} books; {pending} waiting for approval"
    return f"Saved requests for {count} books; choose releases to continue"


def unauthorized_grant(actor, permissions: int, *, current: int | None = None) -> str | None:
    """Non-administrators may only add permission bits they already hold."""
    if not actor or actor.role == "admin":
        return None
    if permissions & ADMIN:
        return "Only an administrator can grant administrator access"
    held = effective_permissions(actor)
    added = permissions if current is None else permissions & ~current
    if added & ~held:
        return "You can only grant permissions you already have"
    return None


def auto_approves(user, spec=None) -> bool:
    perms = effective_permissions(user)
    if perms & AUTO_APPROVE:
        return True
    if spec is None:
        return bool(perms & AUTO_APPROVE_EBOOK) and bool(perms & AUTO_APPROVE_AUDIO)
    needed = media_of(spec)
    if "ebook" in needed and not perms & AUTO_APPROVE_EBOOK:
        return False
    if "audio" in needed and not perms & AUTO_APPROVE_AUDIO:
        return False
    return True


def prepare_manual_approval(user, resolved, reason, explicit, original, preference_choice):
    assert_can_request(user, resolved, explicit, original, preference_choice)
    if auto_approves(user, resolved):
        if reason.approval_status != "approved":
            reason.approval_status = "approved"
            reason.decided_by = user.id
            reason.decided_at = datetime.now(UTC)
            reason.decision_note = None
        elif reason.decided_by is None:
            reason.decided_by = user.id
            reason.decided_at = reason.decided_at or datetime.now(UTC)
        return
    if reason.approval_status == "approved" and reason.decided_by not in (None, user.id):
        return
    reason.approval_status = "pending"
    reason.decided_by = None
    reason.decided_at = None
    reason.decision_note = None


async def require_download_allowed(db, user, intent):
    from sqlalchemy import select

    from app.db.models import AcquisitionReason
    from app.domain.acquisition import RequestSpec

    spec = RequestSpec.model_validate(intent.specification)
    reasons = (
        await db.scalars(
            select(AcquisitionReason).where(
                AcquisitionReason.intent_id == intent.id,
                AcquisitionReason.active.is_(True),
            )
        )
    ).all()
    statuses = [reason.approval_status for reason in reasons]
    # A withdrawn request has no active reason. Preparation then reports that
    # the target is no longer wanted, rather than calling the withdrawal a decline.
    if reasons and "approved" not in statuses:
        if "pending" in statuses:
            raise HTTPException(403, "This request is waiting for approval")
        raise HTTPException(403, "This request was declined")
    if not auto_approves(user, spec):
        raise HTTPException(
            403, "Your account can request this book, but cannot start the download"
        )


def coerce_recovery_permissions(role: str, can_automate: bool, permissions: int | None) -> int:
    if role == "viewer":
        return 0
    if role == "admin":
        return ALL
    base = MEMBER if permissions in (None, 0) else int(permissions)
    base &= ~ADMIN
    if can_automate:
        base |= AUTOMATE
    else:
        base &= ~AUTOMATE
    return base or (MEMBER | (AUTOMATE if can_automate else 0))
