"""Owner-scoped author/series follows backed by ordinary list policy authority."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.adapters.hardcover_follows import FollowFilters
from app.api.dependencies import Database, Member
from app.api.list_subscriptions import SubscriptionView
from app.api.list_subscriptions import view as subscription_view
from app.db.models import AuditEvent, BookList, CatalogAccount, ListSubscription, Operation
from app.domain import list_policies
from app.domain.follows import source
from app.domain.list_subscriptions import begin, owned_list
from app.domain.operations import transaction_lock
from app.security import decrypt_secrets, encrypt_secrets

router = APIRouter(prefix="/following", tags=["following"])


class CatalogFollowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: Literal["author", "series"]
    external_id: int = Field(ge=1, le=2147483647, strict=True)
    name: str = Field(min_length=1, max_length=200)
    filters: FollowFilters = Field(default_factory=FollowFilters)


class FollowEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    enabled: bool
    filters: FollowFilters


class CatalogFollowView(BaseModel):
    list_id: UUID
    name: str
    source_kind: Literal["author", "series"]
    external_id: str
    filters: FollowFilters
    subscription: SubscriptionView
    mode: str
    active: bool


async def view(db, item, row, config):
    policy = await list_policies.current_policy(db, item.id)
    return CatalogFollowView(
        list_id=item.id,
        name=config.get("name") or item.name,
        source_kind=config["source_kind"],
        external_id=config["external_id"],
        filters=config.get("filters", {}),
        subscription=await subscription_view(db, row),
        mode=policy.configuration["mode"] if policy else "browse",
        active=bool(policy and policy.active),
    )


@router.get("", response_model=list[CatalogFollowView])
async def following(user: Member, db: Database):
    rows = (
        await db.execute(
            select(BookList, ListSubscription)
            .join(ListSubscription)
            .where(BookList.owner_id == user.id, ListSubscription.provider == "hardcover")
            .order_by(BookList.name, BookList.id)
        )
    ).all()
    items = []
    for item, row in rows:
        config = decrypt_secrets(row.encrypted_config)
        if config.get("source_kind") and not config.get("unfollowed"):
            items.append(await view(db, item, row, config))
    await db.commit()
    return items


@router.post("", response_model=CatalogFollowView, status_code=201)
async def follow(body: CatalogFollowInput, user: Member, db: Database):
    await transaction_lock(db, f"follows:{user.id}")
    account = await db.get(CatalogAccount, user.id)
    if not account or not account.enabled:
        raise HTTPException(409, "Connect and enable your Hardcover account in Metadata first")
    rows = (
        await db.execute(
            select(BookList, ListSubscription)
            .join(ListSubscription)
            .where(BookList.owner_id == user.id, ListSubscription.provider == "hardcover")
        )
    ).all()
    for item, row in rows:
        config = decrypt_secrets(row.encrypted_config)
        if config.get("source_kind") == body.source_kind and config["external_id"] == str(
            body.external_id
        ):
            if config.get("unfollowed"):
                await owned_list(db, user, item.id)
                row.generation += 1
                row.enabled, row.next_sync_at = True, datetime.now(UTC)
                config.update(unfollowed=False, complete=False)
                row.encrypted_config = encrypt_secrets(config)
                await begin(db, user, item.id, f"follow:{row.id}:{row.generation}")
            result = await view(db, item, row, config)
            await db.commit()
            return result
    item = BookList(owner_id=user.id, name=body.name, shared=False)
    db.add(item)
    await db.flush()
    config = {
        "source_kind": body.source_kind,
        "external_id": str(body.external_id),
        "name": body.name,
        "filters": body.filters.model_dump(mode="json"),
    }
    row = ListSubscription(
        list_id=item.id,
        provider="hardcover",
        source_kind=body.source_kind,
        encrypted_config=encrypt_secrets(config),
        interval_minutes=1440,
        next_sync_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    await begin(db, user, item.id, f"follow:{row.id}:{row.generation}")
    db.add(AuditEvent(actor_id=user.id, action="follow.created", entity_id=row.id))
    result = await view(db, item, row, config)
    await db.commit()
    return result


@router.patch("/{list_id}", response_model=CatalogFollowView)
async def edit(list_id: UUID, body: FollowEdit, user: Member, db: Database):
    item = await owned_list(db, user, list_id)
    found = await source(db, list_id)
    if not found or found[1].get("unfollowed"):
        raise HTTPException(404, "Follow not found")
    row, config = found
    if row.generation != body.expected_generation:
        raise HTTPException(409, "Follow settings changed; reload before saving")
    policy = await list_policies.current_policy(db, list_id)
    if policy:
        await list_policies.pause(db, user, list_id, policy.revision)
    if row.operation_id:
        operation = await db.get(Operation, row.operation_id)
        if operation and operation.status in {"queued", "running"}:
            operation.status, operation.message = "failed", "Follow settings changed"
    config.update(filters=body.filters.model_dump(mode="json"), complete=False)
    row.encrypted_config = encrypt_secrets(config)
    row.generation += 1
    row.enabled = body.enabled
    row.state, row.message = (
        ("idle", "Refresh and preview the follow policy")
        if body.enabled
        else ("paused", "Follow paused")
    )
    row.run_token, row.operation_id = None, None
    row.next_sync_at = datetime.now(UTC) if body.enabled else None
    if body.enabled:
        await begin(db, user, list_id, f"follow:{row.id}:{row.generation}")
    db.add(AuditEvent(actor_id=user.id, action="follow.updated", entity_id=row.id))
    result = await view(db, item, row, config)
    await db.commit()
    return result


@router.delete("/{list_id}", status_code=204)
async def unfollow(list_id: UUID, user: Member, db: Database):
    await owned_list(db, user, list_id)
    found = await source(db, list_id)
    if not found:
        raise HTTPException(404, "Follow not found")
    row, config = found
    policy = await list_policies.current_policy(db, list_id)
    if policy:
        await list_policies.pause(db, user, list_id, policy.revision)
        await list_policies.withdraw_generation(db, user, policy)
    row.enabled, row.next_sync_at, row.run_token = False, None, None
    row.generation += 1
    row.state, row.message = "paused", "Unfollowed; books and exclusions preserved"
    row.encrypted_config = encrypt_secrets({**config, "unfollowed": True})
    db.add(AuditEvent(actor_id=user.id, action="follow.removed", entity_id=row.id))
    await db.commit()
