"""Atomic local following with the existing subscription worker and no acquisition consent."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.adapters.hardcover_lists import MAX_MEMBERS
from app.config import get_settings
from app.db.models import AuditEvent, BookList, CatalogAccount, ListSubscription, Operation, User
from app.domain.list_subscriptions import begin
from app.domain.operations import transaction_lock
from app.security import decrypt_secrets, encrypt_secrets

KIND = "discovery.follow-list"


class FollowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")


class FollowResult(BaseModel):
    list_id: UUID
    subscription_id: UUID
    reused: bool
    receipt_id: UUID


async def followed(db, owner_id):
    result = {}
    for item, subscription in await db.execute(
        select(BookList, ListSubscription)
        .join(ListSubscription, ListSubscription.list_id == BookList.id)
        .where(BookList.owner_id == owner_id, ListSubscription.provider == "hardcover")
        .order_by(BookList.created_at, BookList.id)
    ):
        config = decrypt_secrets(subscription.encrypted_config)
        if config.get("source_kind"):
            continue
        external = config.get("external_id")
        if external:
            result.setdefault(external, (item, subscription))
    return result


async def replay(db, owner_id, key, command):
    old = await db.scalar(
        select(Operation).where(Operation.owner_id == owner_id, Operation.idempotency_key == key)
    )
    if not old:
        return None
    if old.kind != KIND or old.payload.get("command") != command:
        raise HTTPException(409, "This operation key was used for a different command")
    result = FollowResult.model_validate(old.payload["result"])
    item = await db.get(BookList, result.list_id)
    subscription = await db.get(ListSubscription, result.subscription_id)
    if (
        not item
        or item.owner_id != owner_id
        or not subscription
        or subscription.list_id != item.id
        or subscription.provider != "hardcover"
        or decrypt_secrets(subscription.encrypted_config).get("external_id")
        != command["external_id"]
    ):
        raise HTTPException(
            409, "That followed list was removed or detached. Start a new follow action."
        )
    return result


async def follow(db, owner_id, key, command, public_list, generation):
    if get_settings().recovery_mode:
        raise HTTPException(409, "New list observations are paused during recovery")
    await transaction_lock(db, f"community-follow:{owner_id}:{command['external_id']}")
    current = (await followed(db, owner_id)).get(command["external_id"])
    if current:
        # Subscription commands take list → operation → account → actor locks.
        item_id, subscription_id = current[0].id, current[1].id
        item = await db.scalar(
            select(BookList)
            .where(BookList.id == item_id, BookList.owner_id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        subscription = await db.get(ListSubscription, subscription_id, populate_existing=True)
        if (
            not item
            or not subscription
            or subscription.provider != "hardcover"
            or decrypt_secrets(subscription.encrypted_config).get("external_id")
            != command["external_id"]
        ):
            raise HTTPException(409, "The existing subscription changed. Retry this follow action.")
    await transaction_lock(db, f"operation:{owner_id}:{key}")
    existing = await replay(db, owner_id, key, command)
    if existing:
        return existing
    await transaction_lock(db, f"catalog-account:{owner_id}")
    account = await db.get(CatalogAccount, owner_id, populate_existing=True)
    if not account or not account.enabled or account.generation != generation:
        raise HTTPException(
            409, "Your Hardcover account changed. Reload the list before following."
        )
    user = await db.scalar(
        select(User)
        .where(User.id == owner_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if not user or not user.active or user.role == "viewer":
        raise HTTPException(403, "You no longer have permission to follow lists")
    if not current:
        if public_list.count > MAX_MEMBERS:
            raise HTTPException(422, "This list exceeds the supported 5,000-membership sync limit.")
        item = BookList(
            owner_id=owner_id,
            name=(command["name"] or public_list.name.strip()[:200]).strip(),
            description=(public_list.description or "")[:3000] or None,
            shared=False,
        )
        db.add(item)
        await db.flush()
        subscription = ListSubscription(
            list_id=item.id,
            provider="hardcover",
            enabled=True,
            interval_minutes=30,
            encrypted_config=encrypt_secrets(
                {"external_id": command["external_id"], "name": public_list.name}
            ),
            next_sync_at=datetime.now(UTC),
            message="Waiting to observe the community list",
        )
        db.add(subscription)
        await db.flush()
    receipt = Operation(
        owner_id=owner_id,
        kind=KIND,
        idempotency_key=key,
        status="completed",
        message="Opened an existing followed list"
        if current
        else "Following a community list; downloads remain off",
    )
    db.add(receipt)
    await db.flush()
    result = FollowResult(
        list_id=item.id,
        subscription_id=subscription.id,
        reused=bool(current),
        receipt_id=receipt.id,
    )
    receipt.payload = {"command": command, "result": result.model_dump(mode="json")}
    if not current:
        sync = await begin(db, user, item.id, f"community-sync:{receipt.id}")
        receipt.payload = {**receipt.payload, "sync_operation_id": str(sync.id)}
    db.add(AuditEvent(actor_id=owner_id, action="list.community.followed", entity_id=item.id))
    return result
