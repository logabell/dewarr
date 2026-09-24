"""Route committed events, batch discoveries, and fence outbound attempts before I/O."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    NotificationPolicy,
    RestoreCheckpoint,
    User,
)
from app.db.session import session_factory
from app.domain.permissions import MANAGE_REQUESTS, has
from app.notifications import channels
from app.notifications.events import DEFAULT_MEMBER_EVENTS, EVENTS, public_payload
from app.security import decrypt_secrets


async def allowed_events(db, user):
    if user.role == "admin":
        return list(EVENTS)
    policy = await db.get(NotificationPolicy, 1)
    allowed = policy.member_events if policy else DEFAULT_MEMBER_EVENTS
    return [event for event in allowed if event != "request.pending" or has(user, MANAGE_REQUESTS)]


async def eligible(db, channel, event):
    if not channel.enabled:
        return False
    if event.event_type == "test":
        actor = await db.get(User, event.owner_id, populate_existing=True)
        return bool(
            actor
            and actor.active
            and (
                channel.owner_id == actor.id or (channel.owner_id is None and actor.role == "admin")
            )
        )
    if event.event_type not in channel.events:
        return False
    if channel.owner_id is None:
        # Installation destinations are explicitly administered, and contain no
        # account identifiers. Private discovery activity stays personal.
        return not event.event_type.startswith("discovery.")
    user = await db.get(User, channel.owner_id, populate_existing=True)
    if not user or not user.active or event.event_type not in await allowed_events(db, user):
        return False
    if event.event_type == "request.pending":
        return has(user, MANAGE_REQUESTS)
    return event.owner_id == user.id


async def route_events(db):
    now = datetime.now(UTC)
    events = list(
        await db.scalars(
            select(NotificationEvent)
            .where(NotificationEvent.routed_at.is_(None))
            .order_by(NotificationEvent.created_at)
            .limit(500)
            .with_for_update(skip_locked=True)
        )
    )
    destinations = list(
        await db.scalars(select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))
    )
    for event in events:
        for channel in destinations:
            if channel.created_at > event.created_at or not await eligible(db, channel, event):
                continue
            minutes = channel.digest_minutes if event.event_type.startswith("discovery.") else 0
            # Align discovery windows so one sync produces one digest per destination.
            due = (
                datetime.fromtimestamp(
                    ((now.timestamp() // (minutes * 60)) + 1) * minutes * 60, UTC
                )
                if minutes
                else now
            )
            db.add(
                NotificationDelivery(
                    channel_id=channel.id,
                    event_id=event.id,
                    generation=channel.generation,
                    due_at=due,
                )
            )
        event.routed_at = now


def envelope(events, batch_id):
    output = []
    for event in events:
        clean = public_payload(**event.payload)
        output.append(
            {
                "id": str(event.id),
                "type": event.event_type,
                "occurred_at": event.created_at.isoformat(),
                "title": EVENTS.get(event.event_type, clean["title"]),
                "message": clean["message"],
                "url": get_settings().public_url.rstrip("/") + clean["path"],
                **({"cover_url": clean["cover_url"]} if "cover_url" in clean else {}),
            }
        )
    return {"schema_version": 1, "delivery_id": str(batch_id), "events": output}


async def deliver_one():
    factory = session_factory()
    now = datetime.now(UTC)
    async with factory() as db, db.begin():
        first = await db.scalar(
            select(NotificationDelivery)
            .where(NotificationDelivery.state == "pending", NotificationDelivery.due_at <= now)
            .order_by(NotificationDelivery.due_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not first:
            return False
        channel = await db.get(NotificationChannel, first.channel_id)
        first_event = await db.get(NotificationEvent, first.event_id)
        rows = [first]
        if first_event.event_type.startswith("discovery.") and channel.digest_minutes:
            rows = list(
                await db.scalars(
                    select(NotificationDelivery)
                    .join(NotificationEvent)
                    .where(
                        NotificationDelivery.channel_id == channel.id,
                        NotificationDelivery.state == "pending",
                        NotificationDelivery.due_at == first.due_at,
                        NotificationEvent.event_type.like("discovery.%"),
                    )
                    .order_by(NotificationDelivery.id)
                    .limit(200)
                    .with_for_update(of=NotificationDelivery, skip_locked=True)
                )
            )
        selected, events = [], []
        batch_id = uuid4()
        for row in rows:
            event = await db.get(NotificationEvent, row.event_id)
            if (
                row.generation != channel.generation
                or not await eligible(db, channel, event)
                or not channel.enabled
            ):
                row.state = "cancelled"
                row.message = "Channel or permissions changed before delivery"
                continue
            row.state = "sending"
            row.attempted_at = now
            row.batch_id = batch_id
            selected.append(row.id)
            events.append(event)
        if not selected:
            return True
        owner = await db.get(User, channel.owner_id) if channel.owner_id else None
        private_allowed = channel.owner_id is None or bool(owner and owner.role == "admin")
        encrypted_config = channel.encrypted_secrets
        kind = channel.kind
        payload = envelope(events, batch_id)
    # 'sending' commits before external I/O. A restart cannot repeat this attempt.
    state, message = "sent", "Delivered"
    try:
        await asyncio.wait_for(
            channels.send(
                kind, decrypt_secrets(encrypted_config), payload, private_allowed=private_allowed
            ),
            timeout=45,
        )
    except channels.DeliveryError as error:
        state, message = "failed", str(error)
    except (httpx.TransportError, TimeoutError):
        state, message = (
            "uncertain",
            "Delivery was interrupted; automatic replay is disabled to avoid duplicates",
        )
    except Exception:
        state, message = (
            "failed",
            "Channel configuration or delivery failed; check the destination and test again",
        )
    async with factory() as db, db.begin():
        await db.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id.in_(selected), NotificationDelivery.state == "sending")
            .values(state=state, message=message, finished_at=datetime.now(UTC))
        )
    return True


async def tick():
    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        from app.recovery import restore_pending

        if await restore_pending(db):
            return
        await db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.state == "sending",
                NotificationDelivery.attempted_at < datetime.now(UTC) - timedelta(minutes=2),
            )
            .values(
                state="uncertain",
                message="Worker stopped during delivery; automatic replay is disabled",
                finished_at=datetime.now(UTC),
            )
        )
        boundary = await db.scalar(select(func.max(RestoreCheckpoint.created_at)))
        if boundary:
            await db.execute(
                update(NotificationEvent)
                .where(
                    NotificationEvent.created_at <= boundary,
                    NotificationEvent.routed_at.is_(None),
                )
                .values(routed_at=datetime.now(UTC))
            )
            await db.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.created_at <= boundary,
                    NotificationDelivery.state == "pending",
                )
                .values(state="cancelled", message="Restored notifications are not replayed")
            )
        await route_events(db)
    for _ in range(20):
        if not await deliver_one():
            break
