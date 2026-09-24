"""Account-level shelf discovery layered over durable list subscriptions."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.adapters import goodreads_profile, storygraph
from app.adapters.contracts import AdapterError
from app.api.dependencies import Database, Member
from app.api.list_subscriptions import SubscriptionView
from app.api.list_subscriptions import view as subscription_view
from app.api.metadata import adapter_http_error, current_actor, provider_call
from app.config import get_settings
from app.db.models import (
    AuditEvent,
    BookList,
    GoodreadsAccount,
    ListSubscription,
    StorygraphAccount,
)
from app.domain.list_subscriptions import begin
from app.domain.operations import transaction_lock
from app.security import decrypt_secrets, encrypt_secrets

router = APIRouter(prefix="/reading-accounts", tags=["reading-accounts"])


class GoodreadsConnect(BaseModel):
    profile: str = Field(min_length=1, max_length=2000)

    @field_validator("profile")
    @classmethod
    def valid_profile(cls, value):
        goodreads_profile.profile_input(value)
        return value


class GoodreadsShelf(BaseModel):
    external_id: str
    name: str
    count: int | None


class GoodreadsAccountView(BaseModel):
    user_id: str
    name: str
    profile_url: str
    selected: str | None
    shelves: list[GoodreadsShelf]
    discovered_at: datetime
    warning: str | None


class StorygraphConnect(BaseModel):
    session_cookie: str = Field(min_length=8, max_length=4096)
    remember_token: str = Field(min_length=8, max_length=4096)


class StorygraphShelf(BaseModel):
    external_id: str
    name: str
    count: int | None
    kind: Literal["shelf", "tag"]


class StorygraphAccountView(BaseModel):
    username: str
    profile_url: str
    shelves: list[StorygraphShelf]
    discovered_at: datetime


class ReadingSubscription(BaseModel):
    list_id: UUID
    name: str
    external_id: str
    account_id: str | None
    subscription: SubscriptionView


class FollowReadingList(BaseModel):
    provider: Literal["goodreads", "hardcover", "storygraph"]
    external_id: str = Field(min_length=1, max_length=200)
    interval_minutes: int = Field(default=60, ge=30, le=1440)


class ReadingFollowResult(BaseModel):
    list_id: UUID
    reused: bool


def account_view(row):
    config = decrypt_secrets(row.encrypted_config)
    return GoodreadsAccountView(
        user_id=config["user_id"],
        name=config["name"],
        profile_url=f"https://www.goodreads.com/user/show/{config['user_id']}",
        selected=config.get("selected"),
        shelves=config["shelves"],
        discovered_at=row.discovered_at,
        warning=config.get("warning"),
    )


def storygraph_view(row):
    config = decrypt_secrets(row.encrypted_config)
    return StorygraphAccountView(
        username=config["username"],
        profile_url=f"https://app.thestorygraph.com/profile/{config['username']}",
        shelves=config["shelves"],
        discovered_at=row.discovered_at,
    )


@router.get("/goodreads", response_model=GoodreadsAccountView | None)
async def goodreads_account(user: Member, db: Database):
    row = await db.get(GoodreadsAccount, user.id)
    return account_view(row) if row else None


async def save_discovery(db, user, config, expected=None):
    user_id = user.id
    await db.rollback()
    try:
        result = await goodreads_profile.discover(config)
    except AdapterError as error:
        raise adapter_http_error(error) from error
    await transaction_lock(db, f"goodreads-account:{user_id}")
    user = await current_actor(db, user_id, edit=True)
    row = await db.get(GoodreadsAccount, user_id, populate_existing=True)
    if expected is not None and (not row or row.encrypted_config != expected):
        raise HTTPException(409, "Your Goodreads connection changed. Reload before checking again.")
    if not row:
        row = GoodreadsAccount(user_id=user.id)
        db.add(row)
    row.encrypted_config = encrypt_secrets(result)
    row.discovered_at = datetime.now(UTC)
    db.add(AuditEvent(actor_id=user.id, action="goodreads.account.connected", entity_id=user.id))
    await db.commit()
    return account_view(row)


@router.put("/goodreads", response_model=GoodreadsAccountView)
async def connect_goodreads(body: GoodreadsConnect, user: Member, db: Database):
    return await save_discovery(db, user, goodreads_profile.profile_input(body.profile))


@router.post("/goodreads/discover", response_model=GoodreadsAccountView)
async def discover_goodreads(user: Member, db: Database):
    row = await db.get(GoodreadsAccount, user.id)
    if not row:
        raise HTTPException(409, "Connect Goodreads first")
    expected = row.encrypted_config
    return await save_discovery(db, user, decrypt_secrets(expected), expected)


async def retarget_storygraph_lists(db, user_id, previous, username):
    if not previous or previous == username:
        return
    rows = await db.scalars(
        select(ListSubscription)
        .join(BookList, BookList.id == ListSubscription.list_id)
        .where(BookList.owner_id == user_id, ListSubscription.provider == "storygraph")
    )
    for sub in rows:
        config = decrypt_secrets(sub.encrypted_config)
        if config.get("source_kind"):
            continue
        if config.get("username") != previous:
            continue
        config["username"] = username
        sub.encrypted_config = encrypt_secrets(config)


async def save_storygraph(db, user, secret, expected=None):
    from app.db.session import session_factory
    from app.domain.storygraph_subscriptions import (
        BUSY,
        LIMITED,
        fetch_lock,
        save_rotation,
        storygraph_budget,
    )

    user_id = user.id
    await db.rollback()
    async with fetch_lock(user_id, wait=False) as acquired:
        if not acquired:
            raise HTTPException(429, BUSY)
        async with session_factory()() as gate, gate.begin():
            wait = await storygraph_budget(gate, user_id)
        if wait:
            raise HTTPException(429, LIMITED)
        if expected is not None:
            row = await db.get(StorygraphAccount, user_id)
            if not row or row.encrypted_config != expected:
                raise HTTPException(
                    409, "Your StoryGraph connection changed. Reload before checking again."
                )
            try:
                secret = storygraph.open_session(decrypt_secrets(row.encrypted_config))
            except AdapterError as error:
                raise adapter_http_error(error) from error
            expected = row.encrypted_config
            await db.rollback()
        live = {}
        try:
            result = await storygraph.discover(secret, session_out=live)
        except AdapterError as error:
            await save_rotation(db, user_id, secret["session_cookie"], live.get("session_cookie"))
            await db.commit()
            raise adapter_http_error(error) from error
        await transaction_lock(db, f"storygraph-account:{user_id}")
        user = await current_actor(db, user_id, edit=True)
        row = await db.get(StorygraphAccount, user_id, populate_existing=True)
        if expected is not None and (not row or row.encrypted_config != expected):
            await save_rotation(db, user_id, secret["session_cookie"], result.get("session_cookie"))
            await db.commit()
            raise HTTPException(
                409, "Your StoryGraph connection changed. Reload before checking again."
            )
        previous = decrypt_secrets(row.encrypted_config).get("username") if row else None
        if not row:
            row = StorygraphAccount(user_id=user.id)
            db.add(row)
        row.encrypted_config = encrypt_secrets(result)
        await retarget_storygraph_lists(db, user.id, previous, result["username"])
        row.discovered_at = datetime.now(UTC)
        db.add(
            AuditEvent(actor_id=user.id, action="storygraph.account.connected", entity_id=user.id)
        )
        await db.commit()
        return storygraph_view(row)


@router.get("/storygraph", response_model=StorygraphAccountView | None)
async def storygraph_account(user: Member, db: Database):
    row = await db.get(StorygraphAccount, user.id)
    return storygraph_view(row) if row else None


@router.put("/storygraph", response_model=StorygraphAccountView)
async def connect_storygraph(body: StorygraphConnect, user: Member, db: Database):
    try:
        secret = storygraph.cookies(body.session_cookie, body.remember_token)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return await save_storygraph(db, user, secret)


@router.post("/storygraph/discover", response_model=StorygraphAccountView)
async def discover_storygraph(user: Member, db: Database):
    row = await db.get(StorygraphAccount, user.id)
    if not row:
        raise HTTPException(409, "Connect StoryGraph first")
    expected = row.encrypted_config
    try:
        secret = storygraph.open_session(decrypt_secrets(expected))
    except AdapterError as error:
        raise adapter_http_error(error) from error
    return await save_storygraph(db, user, secret, expected)


@router.delete("/storygraph", status_code=204)
async def disconnect_storygraph(user: Member, db: Database):
    from app.domain.storygraph_subscriptions import BUSY, fetch_lock

    async with fetch_lock(user.id, wait=False) as acquired:
        if not acquired:
            raise HTTPException(429, BUSY)
        return await _disconnect_storygraph(user, db)


async def _disconnect_storygraph(user, db):
    row = await db.get(StorygraphAccount, user.id)
    if row:
        await db.delete(row)
        db.add(
            AuditEvent(
                actor_id=user.id, action="storygraph.account.disconnected", entity_id=user.id
            )
        )
    await db.commit()


@router.get("/subscriptions", response_model=list[ReadingSubscription])
async def subscriptions(user: Member, db: Database):
    rows = (
        await db.execute(
            select(BookList, ListSubscription)
            .join(ListSubscription, ListSubscription.list_id == BookList.id)
            .where(BookList.owner_id == user.id)
            .order_by(BookList.created_at, BookList.id)
        )
    ).all()
    results = []
    for item, sub in rows:
        config = decrypt_secrets(sub.encrypted_config)
        if config.get("source_kind"):
            continue
        if sub.provider == "goodreads":
            source = goodreads_profile.profile_input(config["url"])
            account_id, external_id = source["user_id"], source["selected"] or "all"
        elif sub.provider == "storygraph":
            account_id = config.get("username") or "storygraph"
            external_id = config.get("id") or "storygraph"
        else:
            account_id, external_id = None, config["external_id"]
        results.append(
            ReadingSubscription(
                list_id=item.id,
                name=item.name,
                account_id=account_id,
                external_id=external_id,
                subscription=await subscription_view(db, sub),
            )
        )
    await db.commit()
    return results


@router.post("/follow", response_model=ReadingFollowResult)
async def follow(body: FollowReadingList, user: Member, db: Database):
    user_id = user.id
    if get_settings().recovery_mode:
        raise HTTPException(409, "New list observations are paused during recovery")
    if body.provider == "goodreads":
        account = await db.get(GoodreadsAccount, user.id)
        if not account:
            raise HTTPException(409, "Connect Goodreads first")
        config = decrypt_secrets(account.encrypted_config)
        choice = next((s for s in config["shelves"] if s["external_id"] == body.external_id), None)
        if not choice:
            raise HTTPException(422, "Find your Goodreads shelves again before following this one")
        name = choice["name"]
        source_config = {"url": goodreads_profile.shelf_url(config, body.external_id)}
        identity = f"{config['user_id']}:{body.external_id}"
        lock = f"goodreads-follow:{user.id}:{identity}"
    elif body.provider == "storygraph":
        await transaction_lock(db, f"storygraph-account:{user.id}")
        account = await db.get(StorygraphAccount, user.id)
        if not account:
            raise HTTPException(409, "Connect StoryGraph first")
        config = decrypt_secrets(account.encrypted_config)
        choice = next((s for s in config["shelves"] if s["external_id"] == body.external_id), None)
        if not choice:
            raise HTTPException(422, "Find your StoryGraph lists again before following this one")
        name = choice["name"]
        source_config = {
            "kind": choice["kind"],
            "id": choice["external_id"],
            "name": name,
            "username": config["username"],
        }
        identity = storygraph.identity(source_config)
        lock = f"storygraph-follow:{user.id}:{identity}"
    else:
        if (
            not body.external_id.isascii()
            or not body.external_id.isdigit()
            or not (0 < int(body.external_id) <= 2147483647)
        ):
            raise HTTPException(422, "Invalid Hardcover list ID")
        try:
            page, _, _ = await provider_call(
                db, user.id, "hardcover", "list_page", body.external_id, 0, force=True
            )
        except AdapterError as error:
            raise adapter_http_error(error) from error
        user = await current_actor(db, user_id, edit=True)
        name = page.info["name"]
        source_config = {"external_id": body.external_id, "name": name}
        identity = body.external_id
        # Serialize with the existing community-list follow entry point too.
        lock = f"community-follow:{user.id}:{identity}"
    await transaction_lock(db, lock)
    rows = (
        await db.execute(
            select(BookList, ListSubscription)
            .join(ListSubscription, ListSubscription.list_id == BookList.id)
            .where(BookList.owner_id == user.id, ListSubscription.provider == body.provider)
        )
    ).all()
    for item, subscription in rows:
        saved = decrypt_secrets(subscription.encrypted_config)
        if saved.get("source_kind"):
            continue
        if body.provider == "goodreads":
            source = goodreads_profile.profile_input(saved["url"])
            saved_identity = f"{source['user_id']}:{source['selected']}"
        elif body.provider == "storygraph":
            saved_identity = storygraph.identity(saved)
        else:
            saved_identity = saved["external_id"]
        if saved_identity == identity:
            return ReadingFollowResult(list_id=item.id, reused=True)
    item = BookList(owner_id=user.id, name=name[:200], shared=False)
    db.add(item)
    await db.flush()
    subscription = ListSubscription(
        list_id=item.id,
        provider=body.provider,
        encrypted_config=encrypt_secrets(source_config),
        interval_minutes=body.interval_minutes,
        enabled=True,
        next_sync_at=datetime.now(UTC),
    )
    db.add(subscription)
    await db.flush()
    await begin(db, user, item.id, f"reading-follow:{uuid4()}")
    db.add(AuditEvent(actor_id=user.id, action="reading.list.followed", entity_id=item.id))
    await db.commit()
    return ReadingFollowResult(list_id=item.id, reused=False)
