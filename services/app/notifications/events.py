"""Public producer contract: insert in the caller's transaction; never send or commit."""

import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert

from app.db.models import NotificationEvent

EVENTS = {
    "request.pending": "Request needs approval",
    "request.approved": "Request approved",
    "request.declined": "Request declined",
    "download.started": "Download started",
    "import.available": "Available in your library",
    "operation.held": "Held for review",
    "operation.failed": "Operation failed",
    "discovery.list": "New list match",
    "discovery.author": "New author match",
    "discovery.series": "New series match",
    "discovery.gap": "New series gap",
    "connection.problem": "Connection problem",
    "download.stalled": "Download stalled",
    "download.retried": "Download retried",
    "download.gave_up": "Download recovery stopped",
}
DEFAULT_MEMBER_EVENTS = [
    key for key in EVENTS if key not in {"connection.problem", "request.pending"}
]


def safe_text(value: str, limit: int = 500) -> str:
    # Provider errors may contain credentialed URLs. Never forward those URLs or
    # token/cookie assignments, and do not persist response bodies in delivery errors.
    value = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+", "[private URL]", value)
    value = re.sub(
        r"(?i)\b(token|password|secret|cookie|authorization|api[_-]?key)\b\s*[:=].*",
        "[private detail]",
        value,
    )
    return " ".join(value.split())[:limit]


def public_payload(title: str, message: str, path: str, cover_url: str | None = None) -> dict:
    if not re.fullmatch(r"/[a-zA-Z0-9/_#=?&-]*", path) or path.startswith("//"):
        raise ValueError("Notification links must be local application paths")
    if "?" in path and not re.fullmatch(
        r"/organization/inspections\?inspection=[a-f0-9-]{36}", path
    ):
        raise ValueError("Only an inspection identifier is allowed in notification query strings")
    payload = {"title": safe_text(title, 160), "message": safe_text(message), "path": path}
    # Only public catalog covers; authenticated library proxies and signed URLs
    # must never be sent to notification services.
    if cover_url:
        parsed = urlsplit(cover_url)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            in {"assets.hardcover.app", "images-na.ssl-images-amazon.com", "images.gr-assets.com"}
            and not parsed.query
            and not parsed.username
        ):
            payload["cover_url"] = cover_url
    return payload


async def record_event(
    db,
    *,
    key: str,
    event_type: str,
    owner_id: UUID | None,
    subject_id: UUID | None,
    title: str,
    message: str,
    path: str,
    cover_url: str | None = None,
) -> None:
    if event_type not in EVENTS or not key or len(key) > 300:
        raise ValueError("Invalid notification event type or idempotency key")
    if owner_id is None and event_type != "connection.problem":
        raise ValueError("Personal events require an owner")
    await db.execute(
        insert(NotificationEvent)
        .values(
            id=uuid4(),
            key=key,
            event_type=event_type,
            owner_id=owner_id,
            subject_id=subject_id,
            payload=public_payload(title, message, path, cover_url),
        )
        .on_conflict_do_nothing(index_elements=[NotificationEvent.key])
    )
