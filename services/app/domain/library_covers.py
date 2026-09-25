"""Serve library artwork without disclosing backend credentials or filesystem paths."""

import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from uuid import UUID

import httpx
from fastapi import HTTPException, Response
from sqlalchemy import and_, func, select
from sqlalchemy.orm import defer, with_expression

from app.adapters.audiobookshelf import external_id
from app.adapters.contracts import AdapterError
from app.adapters.grimmory import Grimmory
from app.adapters.http import configured_url
from app.db.models import Integration, InventoryItemState, Library, LibraryAsset, User, Version
from app.domain.availability import availability_rows
from app.domain.cache_entries import read_through
from app.domain.catalog_display import display_map
from app.domain.primary_editions import asset_narrators, edition_order, primary_choices
from app.security import decrypt_secrets


async def fetch_cover(
    base_url: str,
    token: str,
    item_id: str,
    *,
    kind: str = "audiobookshelf",
    secrets: dict | None = None,
) -> tuple[bytes, str]:
    if kind == "grimmory":
        try:
            async with Grimmory(base_url, secrets or {}) as client:
                return await client.cover(item_id)
        except AdapterError as error:
            raise HTTPException(404, "Cover unavailable") from error
    try:
        item_id = external_id(item_id)
        async with (
            asyncio.timeout(15),
            httpx.AsyncClient(
                base_url=configured_url(base_url) + "/",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream("GET", f"api/items/{item_id}/cover", params={"width": 480}) as response,
        ):
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code != 200 or kind not in {
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/avif",
                "image/gif",
            }:
                raise HTTPException(404, "Cover unavailable")
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                if len(content) + len(chunk) > 8 * 1024 * 1024:
                    raise HTTPException(404, "Cover unavailable")
                content.extend(chunk)
            return bytes(content), kind
    except (httpx.HTTPError, TimeoutError, AdapterError, ValueError) as error:
        raise HTTPException(404, "Cover unavailable") from error


async def cover_candidates(db, user, work_id: UUID, medium: str):
    mapping = display_map(user, [work_id])
    root = select(mapping.c.work_id).where(mapping.c.origin_id == work_id).scalar_subquery()
    root_id = await db.scalar(select(mapping.c.work_id).where(mapping.c.origin_id == work_id))
    choices = await primary_choices(db, user, mapping, [root_id] if root_id else [])
    rows = await db.execute(
        availability_rows(user, mapping)
        .with_only_columns(
            LibraryAsset, Integration, Version.narrators, InventoryItemState.source_marker
        )
        .outerjoin(Version, Version.id == LibraryAsset.version_id)
        .outerjoin(
            InventoryItemState,
            and_(
                InventoryItemState.integration_id == Integration.id,
                InventoryItemState.library_external_id == Library.external_id,
                InventoryItemState.item_external_id == LibraryAsset.external_id,
            ),
        )
        .options(
            defer(LibraryAsset.files),
            with_expression(
                LibraryAsset.metadata_snapshot,
                func.jsonb_build_object(
                    "cover_path",
                    LibraryAsset.metadata_snapshot["cover_path"],
                    "narrators",
                    LibraryAsset.metadata_snapshot["narrators"],
                ),
            ),
        )
        .execution_options(populate_existing=True)
        .where(
            mapping.c.work_id == root,
            Integration.kind.in_(["audiobookshelf", "grimmory"]),
        )
        .order_by((LibraryAsset.medium == medium).desc(), LibraryAsset.created_at.desc())
    )
    rows = sorted(
        rows.all(),
        key=lambda row: (
            row[0].medium != medium,
            *edition_order(
                row[0], choices.get(root_id, {}).get(row[0].medium), asset_narrators(row[0], row[2])
            ),
        ),
    )
    candidates = []
    for asset, integration, _, revision in rows:
        path = asset.metadata_snapshot.get("cover_path")
        if not path:
            continue
        material = [
            "library-cover-v1",
            str(integration.id),
            integration.credential_generation,
            integration.base_url,
            asset.external_id,
            path,
            revision,
        ]
        key = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        candidates.append(
            (
                key,
                integration.base_url,
                integration.kind,
                integration.encrypted_secrets,
                asset.external_id,
            )
        )
    return candidates


async def library_cover(db, user, work_id: UUID, medium: str, if_none_match=None):
    user_id = user.id
    candidates = await cover_candidates(db, user, work_id, medium)
    await db.rollback()
    for key, url, backend, encrypted, external in candidates:

        async def load(url=url, encrypted=encrypted, external=external, backend=backend, key=key):
            secrets = decrypt_secrets(encrypted)
            data, kind = await fetch_cover(
                url, secrets.get("token", ""), external, kind=backend, secrets=secrets
            )
            return {
                "body": base64.b64encode(data).decode("ascii"),
                "type": kind,
                "etag": '"' + hashlib.sha256(key.encode() + data).hexdigest() + '"',
            }

        try:
            cached, _ = await read_through(
                key, load, fresh_for=timedelta(days=1), allow_stale=False
            )
        except HTTPException:
            continue
        except AdapterError as error:
            raise HTTPException(
                503, str(error), headers={"Retry-After": str(error.retry_after or 1)}
            ) from error
        # Re-evaluate grants, integration generation and selected artwork after I/O.
        user = await db.get(User, user_id, populate_existing=True)
        if not user or not user.active:
            raise HTTPException(404, "Cover unavailable")
        current = await cover_candidates(db, user, work_id, medium)
        await db.rollback()
        if key not in {candidate[0] for candidate in current}:
            raise HTTPException(404, "Cover unavailable")
        etag = cached.get("etag")
        if not etag:
            etag = (
                '"'
                + hashlib.sha256(key.encode() + base64.b64decode(cached["body"])).hexdigest()
                + '"'
            )
        headers = {
            "Cache-Control": "private, no-cache",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
        }
        tags = {tag.strip().removeprefix("W/") for tag in (if_none_match or "").split(",")}
        if etag in tags or "*" in tags:
            return Response(status_code=304, headers=headers)
        return Response(
            base64.b64decode(cached["body"]), media_type=cached["type"], headers=headers
        )
    raise HTTPException(404, "Cover unavailable")
