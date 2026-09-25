from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.dependencies import CurrentUser, Database
from app.db.models import Integration, SourceConnection
from app.domain.connection_health import SOURCE_NAMES, connection_status, proxy_snapshot
from app.domain.downloaders import DOWNLOAD_KINDS
from app.security import decrypt_secrets

router = APIRouter(prefix="/health", tags=["health"])


class ConnectionHealthItem(BaseModel):
    key: str
    name: str
    status: str
    message: str
    checked_at: datetime | None
    settings_url: str | None


class ConnectionHealthView(BaseModel):
    connections: list[ConnectionHealthItem]
    issues: int
    check_interval_seconds: int = 300


def message_for(status):
    return {
        "connected": "Connection verified.",
        "stale": "Connection check is overdue. Check that the background worker is running.",
        "untested": "Waiting for the first connection check.",
        "authentication": "Credentials were rejected. Update the saved credentials and test again.",
        "timeout": "The connection timed out.",
        "rate_limit": "The service is rate limiting requests. Checks resume after its cooldown.",
    }.get(status, "Connection failed. Open settings for details and test again.")


@router.get("/connections", response_model=ConnectionHealthView)
async def connections(user: CurrentUser, db: Database):
    admin = user.role == "admin"
    items = []
    sources = (
        await db.scalars(
            select(SourceConnection)
            .where(SourceConnection.enabled.is_(True), SourceConnection.deleted_at.is_(None))
            .order_by(SourceConnection.key)
        )
    ).all()
    for row in sources:
        if row.key not in SOURCE_NAMES:
            continue
        status = connection_status(row)
        message = message_for(status)
        if row.key == "mam":
            if not decrypt_secrets(row.encrypted_secrets).get("mam_id"):
                status, message = "authentication", "Enter mam_id in MAM settings to connect."
            elif row.lease_token and row.lease_until and row.lease_until <= datetime.now(UTC):
                status, message = (
                    "authentication",
                    "MAM session was interrupted. Enter a current mam_id to reconnect.",
                )
            elif status == "authentication":
                message = (
                    "MAM rejected mam_id. Check the proxy IP and renew the session "
                    "for the current connection."
                )
        items.append(
            ConnectionHealthItem(
                key=row.key,
                name=SOURCE_NAMES[row.key],
                status=status,
                message=message,
                checked_at=row.last_checked_at,
                settings_url="/settings#sources" if admin else None,
            )
        )
        if row.key == "mam" and row.proxy_url:
            status, checked, detail = proxy_snapshot(row)
            items.append(
                ConnectionHealthItem(
                    key="mam-proxy",
                    name="MAM proxy",
                    status=status,
                    message=(detail if admin and status != "stale" else message_for(status)),
                    checked_at=checked,
                    settings_url="/settings#sources" if admin else None,
                )
            )
    clients = (
        await db.scalars(
            select(Integration)
            .where(
                Integration.enabled.is_(True),
                Integration.deleted_at.is_(None),
                Integration.owner_id.is_(None),
                Integration.kind.in_(DOWNLOAD_KINDS),
            )
            .order_by(Integration.name, Integration.id)
        )
    ).all()
    for row in clients:
        status = connection_status(row)
        items.append(
            ConnectionHealthItem(
                key=str(row.id),
                name=row.name if admin else "Download client",
                status=status,
                message=message_for(status),
                checked_at=row.last_checked_at,
                settings_url="/settings#downloaders" if admin else None,
            )
        )
    return ConnectionHealthView(
        connections=items, issues=sum(item.status != "connected" for item in items)
    )
