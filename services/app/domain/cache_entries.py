"""Bounded cache fills shared by API and worker processes without locks over I/O."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.adapters.contracts import AdapterError, FailureKind
from app.db.models import ProviderBudget, ProviderCache
from app.db.session import session_factory
from app.domain.operations import transaction_lock

RETENTION = timedelta(days=7)
TRANSIENT = {
    FailureKind.TIMEOUT,
    FailureKind.ROUTE,
    FailureKind.UNAVAILABLE,
    FailureKind.RATE_LIMIT,
}


async def prune(db, now):
    # Expiry ends freshness, not usefulness during a provider outage.
    expired = (
        select(ProviderCache.key)
        .where(ProviderCache.expires_at < now - RETENTION)
        .order_by(ProviderCache.expires_at)
        # A collection-page fill writes up to 100 book lookups plus page/count.
        # Reclaim more than one fill can add so expired storage can catch up.
        .limit(200)
        .with_for_update(skip_locked=True)
    )
    await db.execute(delete(ProviderCache).where(ProviderCache.key.in_(expired)))


async def bounded_load(load):
    try:
        async with asyncio.timeout(50):
            return await load()
    except TimeoutError as error:
        raise AdapterError(
            FailureKind.TIMEOUT, "The cache refresh timed out; retry shortly."
        ) from error


async def read_through(
    key, load, *, fresh_for, force=False, on_stale=None, cacheable=lambda _: True, allow_stale=True
):
    started = datetime.now(UTC)
    deadline = asyncio.get_running_loop().time() + 55
    lease_key = f"cache-fill:{key}"
    while True:
        async with session_factory()() as db:
            now = datetime.now(UTC)
            cached = await db.get(ProviderCache, key)
            value = cached.value if cached else None
            expiry = cached.expires_at if cached else None
            if cached and expiry > now and (not force or cached.fetched_at >= started):
                return value, False
            background = bool(
                cached and not force and on_stale and expiry > now - timedelta(days=1)
            )
        if background:
            if await on_stale():
                return value, True
            on_stale = None
        async with session_factory()() as db, db.begin():
            await transaction_lock(db, lease_key)
            now = datetime.now(UTC)
            # A concurrent owner may have completed since the fast-path read.
            cached = await db.get(ProviderCache, key)
            if cached and cached.expires_at > now and (not force or cached.fetched_at >= started):
                return cached.value, False
            lease = await db.get(ProviderBudget, lease_key)
            until = None
            if not lease or lease.next_request_at <= now:
                until = now + timedelta(seconds=75)
                if lease:
                    lease.next_request_at = until
                else:
                    db.add(ProviderBudget(key=lease_key, next_request_at=until))
        if until:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AdapterError(
                FailureKind.RATE_LIMIT, "Catalog data is being refreshed.", retry_after=1
            )
        await asyncio.sleep(0.1)
    try:
        try:
            result = await bounded_load(load)
        except AdapterError as error:
            if (
                allow_stale
                and value is not None
                and not force
                and expiry > datetime.now(UTC) - RETENTION
                and error.kind in TRANSIENT
            ):
                return value, True
            if error.kind in {
                FailureKind.AUTHENTICATION,
                FailureKind.PERMISSION,
                FailureKind.NOT_FOUND,
            }:
                async with session_factory()() as db, db.begin():
                    await transaction_lock(db, lease_key)
                    lease = await db.get(ProviderBudget, lease_key)
                    if lease and lease.next_request_at == until:
                        await db.execute(delete(ProviderCache).where(ProviderCache.key == key))
            raise
        if cacheable(result):
            async with session_factory()() as db, db.begin():
                await transaction_lock(db, lease_key)
                lease = await db.get(ProviderBudget, lease_key)
                if lease and lease.next_request_at == until:
                    record = await db.get(ProviderCache, key)
                    if not record:
                        record = ProviderCache(key=key)
                        db.add(record)
                    # SQLAlchemy omits unchanged JSON from UPDATE, avoiding large rewrites.
                    record.value = result
                    record.fetched_at = datetime.now(UTC)
                    record.expires_at = record.fetched_at + fresh_for
                    await prune(db, record.fetched_at)
        else:
            # An HTTP-200 error envelope must not leave rejected stale data available
            # to the next background read (notably GraphQL permission failures).
            async with session_factory()() as db, db.begin():
                await transaction_lock(db, lease_key)
                lease = await db.get(ProviderBudget, lease_key)
                if lease and lease.next_request_at == until:
                    await db.execute(delete(ProviderCache).where(ProviderCache.key == key))
        return result, False
    finally:
        async with session_factory()() as db, db.begin():
            await db.execute(
                delete(ProviderBudget).where(
                    ProviderBudget.key == lease_key, ProviderBudget.next_request_at == until
                )
            )
