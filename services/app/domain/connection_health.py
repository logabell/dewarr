"""Durable connection health, using the same guarded tests as Settings."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.contracts import AdapterError
from app.db.models import Integration, RateLimit, SourceConnection, User
from app.db.session import session_factory
from app.domain.mam_diagnostics import probe_egress
from app.domain.operations import transaction_lock
from app.security import decrypt_secrets

CHECK_INTERVAL = timedelta(minutes=5)
STALE_AFTER = timedelta(minutes=10)
SOURCE_NAMES = {
    "mam": "MAM account",
    "prowlarr": "Prowlarr",
    "audiobookbay": "AudiobookBay",
    "slskd": "Soulseek",
}
logger = logging.getLogger(__name__)


def effective_status(status, checked_at, now=None):
    if status == "connected" and (
        checked_at is None or (now or datetime.now(UTC)) - checked_at > STALE_AFTER
    ):
        return "stale"
    return status


def connection_status(row):
    if not row.enabled:
        return "disabled"
    return effective_status(row.status, row.last_checked_at)


def proxy_snapshot(row):
    value = row.proxy_health or {}
    if value.get("generation") != row.generation:
        return "untested", None, "Proxy has not been checked yet."
    raw = value.get("checked_at")
    checked = datetime.fromisoformat(raw) if raw else None
    status = effective_status(value.get("status", "untested"), checked)
    return status, checked, value.get("message", "")


async def check_proxy(generation):
    async with session_factory()() as db:
        row = await db.get(SourceConnection, "mam")
        if not row or not row.enabled or not row.proxy_url or row.generation != generation:
            return
        secrets = decrypt_secrets(row.encrypted_secrets)
        proxy_url = row.proxy_url
    result = await probe_egress(
        proxy_url, secrets.get("proxy_username"), secrets.get("proxy_password")
    )
    await record_proxy(generation, result)


async def record_proxy(generation, result):
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, "source:mam")
        row = await db.get(SourceConnection, "mam")
        if not row or not row.enabled or row.deleted_at or row.generation != generation:
            return
        if (
            result.ip
            and proxy_snapshot(row)[0] == "unavailable"
            and decrypt_secrets(row.encrypted_secrets).get("mam_id")
        ):
            # Working IP lookup cannot clear a failed MAM route. The authenticated
            # test must succeed first, even if it is currently busy/cooling down.
            return
        row.proxy_health = {
            "generation": generation,
            "status": "connected" if result.ip else "unavailable",
            "checked_at": datetime.now(UTC).isoformat(),
            "message": "Proxy connection verified." if result.ip else result.error,
            "ip": result.ip,
        }


async def recheck():
    """Coalesce browser recovery checks across users/processes, with a bounded retry budget.

    A short committed claim avoids holding a DB transaction during network I/O.
    The existing adapter leases still protect requests from overlapping worker/manual tests.
    """
    now = datetime.now(UTC)
    key = "connections:recheck"
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, key)
        claim = await db.get(RateLimit, key)
        if claim and claim.resets_at > now:
            return claim.count == 1
        deadline = now + timedelta(minutes=3)
        if claim:
            claim.count, claim.resets_at = 1, deadline
        else:
            db.add(RateLimit(key=key, count=1, resets_at=deadline))
    try:
        await run(only_issues=True)
    finally:
        async with session_factory()() as db, db.begin():
            await transaction_lock(db, key)
            claim = await db.get(RateLimit, key)
            if claim and claim.resets_at == deadline:
                claim.count = 0
                claim.resets_at = datetime.now(UTC) + timedelta(minutes=1)
    return False


async def run(*, only_issues=False):
    from app.config import get_settings
    from app.domain.audiobookbay_network import abb_call
    from app.domain.downloaders import DOWNLOAD_KINDS, test_connection
    from app.domain.prowlarr_network import prowlarr_call
    from app.domain.slskd_connection import test_connection as test_slskd
    from app.domain.source_network import source_call

    if get_settings().recovery_mode:
        return
    now = datetime.now(UTC)
    async with session_factory()() as db:
        admin_id = await db.scalar(
            select(User.id).where(User.active.is_(True), User.role == "admin").limit(1)
        )
        if admin_id is None:
            return
        sources = (
            await db.scalars(
                select(SourceConnection).where(
                    SourceConnection.enabled.is_(True), SourceConnection.deleted_at.is_(None)
                )
            )
        ).all()
        clients = (
            await db.scalars(
                select(Integration).where(
                    Integration.enabled.is_(True),
                    Integration.deleted_at.is_(None),
                    Integration.owner_id.is_(None),
                    Integration.kind.in_(DOWNLOAD_KINDS),
                )
            )
        ).all()
    semaphore = asyncio.Semaphore(4)

    async def guarded(check):
        async with semaphore:
            try:
                await check()
            except (AdapterError, HTTPException):
                # The existing tests persist failures and respect busy leases/cooldowns.
                pass
            except Exception as error:
                # One broken adapter must not prevent checks of other connections.
                logger.warning("Connection health check failed (%s)", type(error).__name__)

    async def source_check(row):
        if (
            only_issues
            and connection_status(row) == "connected"
            and not (row.key == "mam" and row.proxy_url and proxy_snapshot(row)[0] != "connected")
        ):
            return
        if row.key == "mam":
            # Check egress first: the authenticated request may then expose a
            # MAM-specific proxy failure or direct fallback despite working egress.
            if row.proxy_url:
                await check_proxy(row.generation)
            if only_issues or row.proxy_url or due(row, now):
                await source_call(admin_id, "test", expected_generation=row.generation)
        elif only_issues or due(row, now):
            if row.key == "prowlarr":
                await prowlarr_call(admin_id, "test", expected_generation=row.generation)
            elif row.key == "audiobookbay":
                await abb_call(admin_id, "test", expected_generation=row.generation)
            elif row.key == "slskd":
                await test_slskd(admin_id)

    await asyncio.gather(
        *(guarded(lambda row=row: source_check(row)) for row in sources if row.key in SOURCE_NAMES),
        *(
            guarded(lambda row=row: test_connection(admin_id, row.id))
            for row in clients
            if (connection_status(row) != "connected" if only_issues else due(row, now))
        ),
    )


def due(row, now):
    return (
        row.status == "untested"
        or row.last_checked_at is None
        or now - row.last_checked_at >= CHECK_INTERVAL
    )
