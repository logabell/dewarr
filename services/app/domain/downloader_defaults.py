"""Resolve a protocol's saved choice or its sole configured client."""

from sqlalchemy import select

from app.db.models import Integration
from app.domain.downloaders import TRANSFER_KINDS, client_protocol


async def configured_clients(db):
    # Count configured clients, including those needing verification. A failed
    # connection test must never silently switch a default to a different server.
    return list(
        await db.scalars(
            select(Integration)
            .where(
                Integration.kind.in_(TRANSFER_KINDS),
                Integration.owner_id.is_(None),
                Integration.deleted_at.is_(None),
                Integration.enabled.is_(True),
            )
            .order_by(Integration.created_at, Integration.id)
        )
    )


async def protocol_default(db, preferences, protocol):
    field = {"torrent": "torrent_downloader_id", "nzb": "usenet_downloader_id"}.get(protocol)
    if field and (saved := getattr(preferences, field)):
        return saved
    if preferences.downloader_id:
        legacy = await db.get(Integration, preferences.downloader_id)
        if legacy and client_protocol(legacy.kind) == protocol:
            return legacy.id
    clients = [row for row in await configured_clients(db) if client_protocol(row.kind) == protocol]
    return clients[0].id if len(clients) == 1 else None
