"""Durable, content-validated artwork cache, shared across browser sessions."""

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Response
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.db.models import ProviderBudget, ProviderCache
from app.domain.cache_entries import prune
from app.domain.operations import transaction_lock
from app.importing.covers import CoverError, fetch_cover, normalize_cover, validated_url


async def cached_cover(db, url, size=1200):
    try:
        validated_url(url)
    except CoverError as error:
        raise HTTPException(422, "Unsupported cover URL") from error
    if size not in {320, 640, 1200}:
        raise HTTPException(422, "Unsupported cover size")
    material = f"cover-image:v1:{url}" if size == 1200 else f"cover-thumbnail:v1:{size}:{url}"
    key = hashlib.sha256(material.encode()).hexdigest()
    lease_key = f"cover-fetch:{key}"
    deadline = asyncio.get_running_loop().time() + 55
    # Warm reads do not take an advisory lock or write a transaction.
    cached = await db.get(ProviderCache, key)
    if cached and cached.expires_at > datetime.now(UTC):
        data = base64.b64decode(cached.value["jpeg"])
        await db.rollback()
        return image_response(data)
    while True:
        # A short durable lease coalesces API workers without occupying a database
        # connection or holding an advisory lock during DNS, HTTP or conversion.
        await transaction_lock(db, lease_key)
        now = datetime.now(UTC)
        cached = await db.get(ProviderCache, key, populate_existing=True)
        if cached and cached.expires_at > now:
            data = base64.b64decode(cached.value["jpeg"])
            await db.commit()
            break
        lease = await db.get(ProviderBudget, lease_key, populate_existing=True)
        if lease and lease.next_request_at > now:
            await db.rollback()
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(
                    503, "Cover is being fetched; try again", headers={"Retry-After": "1"}
                )
            await asyncio.sleep(0.2)
            continue
        until = now + timedelta(seconds=60)
        if lease:
            lease.next_request_at = until
        else:
            db.add(ProviderBudget(key=lease_key, next_request_at=until))
        await db.commit()
        try:
            if size == 1200:
                data = await fetch_cover(url)
            else:
                # Variants reuse the durable original and its cross-process fetch lease.
                original = await cached_cover(db, url)
                data = await normalize_cover(original.body, size)
        except BaseException as error:
            await db.execute(
                delete(ProviderBudget).where(
                    ProviderBudget.key == lease_key, ProviderBudget.next_request_at == until
                )
            )
            await db.commit()
            if isinstance(error, CoverError):
                # Never persist a transient failure as an empty cover.
                raise HTTPException(404, "Cover temporarily unavailable") from error
            raise
        await transaction_lock(db, lease_key)
        lease = await db.get(ProviderBudget, lease_key, populate_existing=True)
        if lease and lease.next_request_at == until:
            now = datetime.now(UTC)
            statement = insert(ProviderCache).values(
                key=key,
                value={"jpeg": base64.b64encode(data).decode("ascii")},
                fetched_at=now,
                expires_at=now + timedelta(days=365),
            )
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=["key"],
                    set_={
                        name: getattr(statement.excluded, name)
                        for name in ("value", "fetched_at", "expires_at")
                    },
                )
            )
            await db.delete(lease)
            await prune(db, now)
        await db.commit()
        break
    return image_response(data)


def image_response(data):
    return Response(
        data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
