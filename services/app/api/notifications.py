from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.dependencies import Admin, CurrentUser, Database
from app.db.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    NotificationPolicy,
)
from app.notifications.channels import APPRISE_ADMIN_ONLY, ChannelSecrets, validate_config
from app.notifications.delivery import allowed_events
from app.notifications.events import DEFAULT_MEMBER_EVENTS, EVENTS
from app.security import encrypt_secrets

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["apprise", "discord", "ntfy", "webhook"]
    installation: bool = False
    events: list[str] = Field(max_length=30)
    enabled: bool = True
    digest_minutes: Literal[0, 5, 15, 60, 1440] = 15
    secrets: ChannelSecrets | None = None


class NotificationChannelView(BaseModel):
    id: UUID
    name: str
    kind: str
    installation: bool
    events: list[str]
    enabled: bool
    digest_minutes: int
    configured: bool = True
    last_status: str | None = None
    last_message: str | None = None
    last_delivery_at: datetime | None = None


class NotificationPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_events: list[str] = Field(max_length=30)


class NotificationSettingsView(BaseModel):
    channels: list[NotificationChannelView]
    event_labels: dict[str, str]
    allowed_events: list[str]
    member_events: list[str]


class NotificationDeliveryView(BaseModel):
    id: UUID
    event_type: str
    state: str
    message: str | None
    created_at: datetime
    attempted_at: datetime | None
    finished_at: datetime | None


def scope(user):
    if user.role == "admin":
        return (NotificationChannel.owner_id == user.id) | NotificationChannel.owner_id.is_(None)
    return NotificationChannel.owner_id == user.id


async def owned(db, user, channel_id):
    channel = await db.scalar(
        select(NotificationChannel)
        .where(NotificationChannel.id == channel_id, scope(user))
        .with_for_update()
    )
    if not channel:
        raise HTTPException(404, "Notification channel not found")
    return channel


async def view(db, channel):
    last = await db.scalar(
        select(NotificationDelivery)
        .where(NotificationDelivery.channel_id == channel.id)
        .order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc())
        .limit(1)
    )
    return NotificationChannelView(
        id=channel.id,
        name=channel.name,
        kind=channel.kind,
        installation=channel.owner_id is None,
        events=channel.events,
        enabled=channel.enabled,
        digest_minutes=channel.digest_minutes,
        last_status=last.state if last else None,
        last_message=last.message if last else None,
        last_delivery_at=(last.finished_at or last.attempted_at or last.created_at)
        if last
        else None,
    )


@router.get("", response_model=NotificationSettingsView)
async def settings(user: CurrentUser, db: Database):
    channels = list(
        await db.scalars(
            select(NotificationChannel).where(scope(user)).order_by(NotificationChannel.created_at)
        )
    )
    policy = await db.get(NotificationPolicy, 1)
    return NotificationSettingsView(
        channels=[await view(db, channel) for channel in channels],
        event_labels=EVENTS,
        allowed_events=await allowed_events(db, user),
        member_events=policy.member_events if policy else DEFAULT_MEMBER_EVENTS,
    )


async def apply(db, user, body, channel):
    if body.installation and user.role != "admin":
        raise HTTPException(403, "Only administrators can manage installation channels")
    if (
        body.kind == "apprise"
        and user.role != "admin"
        and (not channel or channel.kind != "apprise" or body.enabled or body.secrets is not None)
    ):
        # Existing owners can still pause/delete an old Apprise channel or switch
        # it to a supported HTTP service, but cannot create or enable one.
        raise HTTPException(403, APPRISE_ADMIN_ONLY)
    allowed = set(EVENTS) if body.installation else set(await allowed_events(db, user))
    if body.enabled and set(body.events) - allowed:
        raise HTTPException(403, "One or more events are not permitted for this channel")
    if body.installation and any(event.startswith("discovery.") for event in body.events):
        raise HTTPException(422, "Private discovery events require a personal channel")
    if not channel and not body.secrets:
        raise HTTPException(422, "Channel credentials are required")
    if channel and channel.kind != body.kind and not body.secrets:
        raise HTTPException(422, "Changing the channel type requires new credentials")
    if body.secrets:
        try:
            validate_config(body.kind, body.secrets)
        except ValueError:
            raise HTTPException(422, "Invalid channel destination or credentials") from None
    if not channel:
        channel = NotificationChannel(owner_id=None if body.installation else user.id, generation=0)
        db.add(channel)
    elif (channel.owner_id is None) != body.installation:
        raise HTTPException(422, "Create a separate channel to change its audience")
    channel.name = body.name.strip()
    channel.kind = body.kind
    channel.events = sorted(set(body.events))
    channel.enabled = body.enabled
    channel.digest_minutes = body.digest_minutes
    channel.generation += 1
    if body.secrets:
        channel.encrypted_secrets = encrypt_secrets(body.secrets.model_dump())
    await db.commit()
    return await view(db, channel)


@router.post("/channels", response_model=NotificationChannelView, status_code=201)
async def create(body: NotificationChannelInput, user: CurrentUser, db: Database):
    count = len(list(await db.scalars(select(NotificationChannel.id).where(scope(user)))))
    if count >= 20:
        raise HTTPException(409, "You can configure up to 20 notification channels")
    return await apply(db, user, body, None)


@router.put("/channels/{channel_id}", response_model=NotificationChannelView)
async def save(channel_id: UUID, body: NotificationChannelInput, user: CurrentUser, db: Database):
    return await apply(db, user, body, await owned(db, user, channel_id))


@router.delete("/channels/{channel_id}", status_code=204)
async def remove(channel_id: UUID, user: CurrentUser, db: Database):
    channel = await owned(db, user, channel_id)
    await db.delete(channel)
    await db.commit()


@router.put("/policy", response_model=NotificationPolicyInput)
async def policy(body: NotificationPolicyInput, admin: Admin, db: Database):
    if set(body.member_events) - EVENTS.keys():
        raise HTTPException(422, "Unknown notification event")
    row = await db.get(NotificationPolicy, 1, with_for_update=True)
    if not row:
        row = NotificationPolicy(id=1)
        db.add(row)
    row.member_events = sorted(set(body.member_events))
    await db.commit()
    return NotificationPolicyInput(member_events=row.member_events)


@router.get("/channels/{channel_id}/deliveries", response_model=list[NotificationDeliveryView])
async def history(channel_id: UUID, user: CurrentUser, db: Database):
    await owned(db, user, channel_id)
    rows = (
        await db.execute(
            select(NotificationDelivery, NotificationEvent.event_type)
            .join(NotificationEvent)
            .where(NotificationDelivery.channel_id == channel_id)
            .order_by(NotificationDelivery.created_at.desc())
            .limit(50)
        )
    ).all()
    return [
        NotificationDeliveryView(
            id=row.id,
            event_type=kind,
            state=row.state,
            message=row.message,
            created_at=row.created_at,
            attempted_at=row.attempted_at,
            finished_at=row.finished_at,
        )
        for row, kind in rows
    ]


@router.post("/channels/{channel_id}/test", response_model=NotificationChannelView, status_code=202)
async def test(channel_id: UUID, user: CurrentUser, db: Database):
    channel = await owned(db, user, channel_id)
    if channel.kind == "apprise" and user.role != "admin":
        raise HTTPException(403, APPRISE_ADMIN_ONLY)
    if not channel.enabled:
        raise HTTPException(409, "Enable this channel before testing")
    recent = await db.scalar(
        select(NotificationDelivery.id)
        .where(
            NotificationDelivery.channel_id == channel.id,
            NotificationDelivery.created_at > datetime.now(UTC) - timedelta(seconds=30),
        )
        .limit(1)
    )
    if recent:
        raise HTTPException(429, "Wait 30 seconds before testing this channel again")
    event = NotificationEvent(
        id=uuid4(),
        key=f"test:{uuid4()}",
        event_type="test",
        owner_id=user.id,
        payload={
            "title": "Dewarr test notification",
            "message": "Your notification channel is working.",
            "path": "/settings#notifications",
        },
        routed_at=datetime.now(UTC),
    )
    db.add(event)
    await db.flush()
    db.add(
        NotificationDelivery(
            channel_id=channel.id,
            event_id=event.id,
            generation=channel.generation,
            due_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return await view(db, channel)
