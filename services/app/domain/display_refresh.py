"""Local display refresh jobs. Resolve current access and source URLs at execution time."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert

from app.db.models import CatalogAccount, ProviderCache
from app.domain.cache_entries import prune


async def save_result(db, key, value, seconds=300):
    now = datetime.now(UTC)
    statement = insert(ProviderCache).values(
        key=key, value=value, fetched_at=now, expires_at=now + timedelta(seconds=seconds)
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
    await prune(db, now)
    await db.commit()


async def run_display(db, owner_id, payload):
    from app.api.metadata import current_actor, reader_match

    user = await current_actor(db, owner_id)
    operation, args = payload["operation"], payload["args"]
    if operation == "reader_match":
        account = await db.get(CatalogAccount, owner_id)
        if not account or not account.enabled or account.generation != payload["generation"]:
            return
        try:
            await reader_match(UUID(args[0]), user, db)
        except HTTPException as error:
            # A failure ends polling; a later visit can retry after the cooldown.
            # The cache key is scoped to actor, credential generation and evidence.
            if error.status_code not in (401, 403, 404):
                await save_result(db, args[1], {"status": "unmatched", "reason": str(error.detail)})
        return
    if operation not in {"collection_page", "collection_snapshot"}:
        raise ValueError("Unsupported display refresh")
    from app.api.discovery_collections import sources

    values, _ = await sources(db, user)
    value = values.get(args[0])
    if not value or value["updated_at"] != args[1]:
        return
    await db.rollback()
    if operation == "collection_page":
        from app.domain.discovery_pages import source_page

        await source_page(db, owner_id, value, args[2], background=False)
    else:
        from app.domain.curation_sources import fetch
        from app.domain.discovery_catalog import save_snapshot

        await save_snapshot(await fetch(value))
