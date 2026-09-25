import hmac
import ipaddress
import logging
import re
import threading
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import LoginSession, User
from app.db.session import database
from app.origins import format_origin, parse_origin
from app.recovery import active_restore, restore_pending
from app.security import csrf_token, token_hash

Database = Annotated[AsyncSession, Depends(database)]
COOKIE = "book_session"
LOG = logging.getLogger(__name__)
_origin_log_times: dict[str, float] = {}
_origin_log_lock = threading.Lock()


def _ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _same_token(left: str, right: str) -> bool:
    if not left or len(left) != len(right):
        return False
    try:
        return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
    except UnicodeEncodeError:
        return False


def _header_values(headers, name: str) -> list[str]:
    getlist = getattr(headers, "getlist", None)
    if getlist is not None:
        return [value for value in getlist(name) if value]
    value = headers.get(name, "")
    return [value] if value else []


def _last_address(values: list[str]) -> str | None:
    found = None
    for value in values:
        for part in value.split(","):
            parsed = _ip(part.strip())
            if parsed:
                found = parsed
    return found


def _forwarded_client(request: Request) -> str | None:
    real = _last_address(_header_values(request.headers, "x-real-ip"))
    last = _last_address(_header_values(request.headers, "x-forwarded-for"))
    if real and last and real != last:
        return None
    return real or last


def _network_client(request: Request, peer: str, networks) -> str:
    if not networks:
        return peer

    def trusted(address: str) -> bool:
        parsed = ipaddress.ip_address(address)
        return any(parsed in network for network in networks)

    if _ip(peer) is None or not trusted(peer):
        return peer
    values = _header_values(request.headers, "x-forwarded-for")
    if values:
        # Walk from the actual peer toward the visitor. Stop at the first untrusted
        # hop; a visitor-controlled prefix must never replace that address.
        addresses = [_ip(part.strip()) for value in values for part in value.split(",")]
        if not addresses or any(address is None for address in addresses):
            return peer
        for address in reversed(addresses):
            if not trusted(address):
                return address
        return addresses[0]
    real = _header_values(request.headers, "x-real-ip")
    return (_ip(real[0].strip()) or peer) if len(real) == 1 else peer


def client_host(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if not peer:
        return "local"
    settings = get_settings()
    configured = settings.proxy_token
    token = configured.get_secret_value() if configured is not None else ""
    if token:
        # Token mode takes precedence. An invalid token cannot fall back to IP trust.
        supplied = _header_values(request.headers, "x-dewarr-proxy-token")
        if not _same_token(token, supplied[-1] if supplied else ""):
            return peer
        return _forwarded_client(request) or peer
    return _network_client(request, peer, settings.proxy_networks)


# Retain the helper name for existing callers; configuration uses the same parser.
_origin = parse_origin


def _log_origin_rejection(request: Request, reason: str, origins, origin, request_origin, public):
    # A fixed set of reason keys bounds memory; one event/minute/reason bounds log volume.
    with _origin_log_lock:
        now = time.monotonic()
        previous = _origin_log_times.get(reason)
        if previous is not None and now - previous < 60:
            return
        _origin_log_times[reason] = now
    LOG.warning(
        "Origin rejected reason=%s request_id=%s origin_count=%s origin=%s "
        "request_origin=%s public_origin=%s",
        reason,
        getattr(request.state, "request_id", "unavailable"),
        len(origins),
        format_origin(origin) if origin else "invalid-or-missing",
        format_origin(request_origin) if request_origin else "invalid",
        format_origin(public) if public else "invalid",
    )


def require_origin(request: Request) -> None:
    origins = request.headers.getlist("origin")
    origin = _origin(origins[0]) if len(origins) == 1 else None
    # TLS termination needs only the configured public origin, not forwarded scheme/host.
    try:
        request_origin = _origin(str(request.url.replace(path="", query="", fragment="")))
    except ValueError:
        request_origin = None
    public = get_settings().public_origin
    if origin is not None and origin in {request_origin, public}:
        return
    if not origins:
        reason, action = "missing", "The proxy must preserve the browser's Origin header."
    elif len(origins) != 1:
        reason, action = "duplicate", "The proxy must forward exactly one Origin header."
    elif origin is None:
        reason, action = "malformed", "The proxy must preserve a valid browser Origin header."
    else:
        reason = "mismatch"
        action = (
            "Set PUBLIC_URL (BOOK_PUBLIC_URL for a native installation) to the browser's "
            "scheme, hostname and port. BOOK_PUBLIC_URL overrides PUBLIC_URL. "
            "Recreate the Docker container after changing environment settings; "
            "restart a native installation."
        )
    _log_origin_rejection(request, reason, origins, origin, request_origin, public)
    raise HTTPException(403, "The request origin is not allowed. " + action)


async def current_user(request: Request, db: Database) -> User:
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(401, "Sign in to continue")
    user = await db.scalar(
        select(User)
        .join(LoginSession, User.id == LoginSession.user_id)
        .where(
            LoginSession.token_hash == token_hash(raw),
            LoginSession.expires_at > datetime.now(UTC),
            User.active.is_(True),
        )
    )
    if not user:
        raise HTTPException(401, "Your session has expired. Sign in again")
    checkpoint = await active_restore(db)
    if checkpoint and user.id != checkpoint.operator_id:
        raise HTTPException(401, "Sign in as the designated recovery operator")
    if await restore_pending(db):
        allowed = {
            ("GET", "/api/auth/me"),
            ("POST", "/api/auth/logout"),
            ("GET", "/api/recovery"),
            ("POST", "/api/recovery/scans"),
            ("POST", "/api/recovery/reconciliations"),
            ("POST", "/api/recovery/inventory-reconciliations"),
            ("POST", "/api/recovery/publication-reconciliations"),
            ("POST", "/api/recovery/list-reconciliations"),
            ("POST", "/api/recovery/outbound-reconciliations"),
            ("POST", "/api/recovery/command-reconciliations"),
            ("POST", "/api/recovery/access-reconciliations"),
            ("POST", "/api/recovery/connection-reconciliations"),
            ("POST", "/api/recovery/source-reconciliations"),
        }
        report_read = request.method == "GET" and bool(
            re.fullmatch(
                r"/api/recovery/scans/[0-9a-f-]{36}(?:/findings/[0-9a-f-]{36})?", request.url.path
            )
        )
        review_action = bool(
            re.fullmatch(
                r"/api/recovery/(?:reconciliations|inventory-reconciliations|publication-reconciliations|list-reconciliations|outbound-reconciliations|command-reconciliations|access-reconciliations|connection-reconciliations|source-reconciliations)/[0-9a-f-]{36}",
                request.url.path,
            )
            and request.method == "GET"
            or re.fullmatch(
                r"/api/recovery/(?:reconciliations|inventory-reconciliations|publication-reconciliations|list-reconciliations|outbound-reconciliations|command-reconciliations|access-reconciliations|connection-reconciliations|source-reconciliations)/[0-9a-f-]{36}/accept",
                request.url.path,
            )
            and request.method == "POST"
        )
        if user.role != "admin" or (
            (request.method, request.url.path) not in allowed
            and not report_read
            and not review_action
        ):
            raise HTTPException(423, "Recovery review is active; application actions are paused")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf_token(raw)):
            raise HTTPException(403, "Refresh this page before trying again")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Administrator access is required")
    return user


Admin = Annotated[User, Depends(require_admin)]


def require_member(user: CurrentUser) -> User:
    if user.role == "viewer":
        raise HTTPException(403, "This account has read-only access")
    return user


Member = Annotated[User, Depends(require_member)]
