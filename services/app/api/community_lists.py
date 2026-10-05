from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from app.adapters.catalog_providers import identifier
from app.adapters.contracts import AdapterError
from app.adapters.hardcover_lists import MAX_MEMBERS
from app.api.dependencies import CurrentUser, Database, Member
from app.api.discovery import DiscoveryItem, project
from app.api.metadata import adapter_http_error, current_actor, provider_call
from app.config import get_settings
from app.db.models import CatalogAccount
from app.domain import community_lists
from app.domain.community_lists import FollowInput, FollowResult

router = APIRouter(prefix="/discovery/lists", tags=["community-lists"])


class CommunityListCard(BaseModel):
    external_id: str
    name: str
    description: str | None
    count: int
    followers: int | None
    updated_at: datetime | None
    covers: list[str]
    followed_list_id: UUID | None = None
    follow_supported: bool


class CommunityListPage(BaseModel):
    items: list[CommunityListCard]
    page: int
    has_more: bool
    warning: str | None = None


class CommunityListPreview(BaseModel):
    info: CommunityListCard
    items: list[DiscoveryItem]
    next_cursor: int | None
    warning: str | None = None


def card(value, follows):
    existing = follows.get(value.external_id)
    return CommunityListCard(
        external_id=value.external_id,
        name=value.name,
        description=value.description,
        count=value.count,
        followers=value.followers,
        updated_at=value.updated_at,
        covers=[book.cover_url for book in value.books if book.cover_url][:4],
        followed_list_id=existing[0].id if existing else None,
        follow_supported=value.count <= MAX_MEMBERS,
    )


async def read(db, user_id, operation, *args, force=False):
    try:
        result, _, _ = await provider_call(
            db, user_id, "hardcover", operation, *args, force=force, background=not force
        )
        return result
    except AdapterError as error:
        raise adapter_http_error(error) from error


def validate_id(external_id):
    try:
        identifier("hardcover", external_id)
    except AdapterError as error:
        raise HTTPException(422, "Invalid Hardcover list identifier") from error


@router.get("", response_model=CommunityListPage)
async def browse(
    user: CurrentUser,
    db: Database,
    q: str = Query(default="", max_length=150),
    page: int = Query(default=1, ge=1, le=50),
):
    user_id = user.id
    value = await read(db, user_id, "community_lists", q, page)
    follows = await community_lists.followed(db, user_id)
    return CommunityListPage(
        items=[card(item, follows) for item in value.items],
        page=page,
        has_more=value.has_more,
        warning=value.warning,
    )


@router.get("/{external_id}", response_model=CommunityListPreview)
async def detail(
    external_id: str,
    user: CurrentUser,
    db: Database,
    cursor: int = Query(default=0, ge=0, le=2147483647),
):
    validate_id(external_id)
    user_id = user.id
    value = await read(db, user_id, "community_list", external_id, cursor)
    user = await current_actor(db, user_id)
    return CommunityListPreview(
        info=card(value, await community_lists.followed(db, user_id)),
        items=await project(db, user, value.books, "Included in this Hardcover community list"),
        next_cursor=value.next_cursor,
        warning=value.warning,
    )


@router.post("/{external_id}/follow", response_model=FollowResult)
async def follow(
    external_id: str,
    body: FollowInput,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    validate_id(external_id)
    if get_settings().recovery_mode:
        raise HTTPException(409, "New list observations are paused during recovery")
    user_id = user.id
    command = {"external_id": external_id, "name": body.name}
    old = await community_lists.replay(db, user_id, idempotency_key, command)
    if old:
        return old
    account = await db.get(CatalogAccount, user_id)
    if not account or not account.enabled:
        raise HTTPException(409, "Connect your Hardcover account before following a list")
    generation = account.generation
    value = await read(db, user_id, "community_list", external_id, 0, force=True)
    result = await community_lists.follow(db, user_id, idempotency_key, command, value, generation)
    await db.commit()
    return result
