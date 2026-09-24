import hmac
import ipaddress
import re
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import LoginSession, User
from app.db.session import database
from app.recovery import active_restore, restore_pending
from app.security import csrf_token, token_hash

Database = Annotated[AsyncSession, Depends(database)]
COOKIE = "book_session"


def _ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _same_token(left: str, right: str) -> bool:
    if not left or len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


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


def client_host(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if not peer:
        return "local"
    configured = get_settings().proxy_token
    token = configured.get_secret_value() if configured is not None else ""
    supplied = _header_values(request.headers, "x-dewarr-proxy-token")
    if not _same_token(token, supplied[-1] if supplied else ""):
        return peer
    return _forwarded_client(request) or peer


def _origin(value: str) -> tuple[str, str, int] | None:
    # Compare browser origins, including default ports, without accepting URL paths
    # or credentials. urlsplit alone silently strips some control characters.
    if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or "?" in value
            or "#" in value
        ):
            return None
        port = parts.port
        return (
            parts.scheme,
            parts.hostname,
            port if port is not None else (443 if parts.scheme == "https" else 80),
        )
    except ValueError:
        return None


def require_origin(request: Request) -> None:
    origins = request.headers.getlist("origin")
    origin = _origin(origins[0]) if len(origins) == 1 else None
    # Direct LAN access may use a different hostname or published port from PUBLIC_URL.
    # The configured origin also supports HTTPS proxies whose upstream is HTTP.
    # Do not derive trusted origins from visitor-supplied forwarding headers.
    request_origin = _origin(str(request.url.replace(path="", query="", fragment="")))
    if origin is None or origin not in {request_origin, _origin(get_settings().public_url)}:
        raise HTTPException(
            403,
            "The request origin is not allowed. Set PUBLIC_URL (BOOK_PUBLIC_URL for a native "
            "installation) to the browser's scheme, hostname and port, then restart Dewarr.",
        )


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
