"""Read-only discovery with scoped catalog bindings and current inventory projection."""

from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.adapters.contracts import AdapterError
from app.api.catalog import WorkView, work_view
from app.api.dependencies import CurrentUser, Database
from app.api.metadata import accessible_work, current_actor, provider_call
from app.db.models import CatalogAccount, LibraryAsset, Work, WorkMetadataSource
from app.domain.availability import availability_for, availability_rows
from app.domain.catalog_bindings import displayed_provider_works
from app.domain.catalog_display import display_ids, display_map
from app.domain.visibility import visible_origin_work, visible_work
from app.domain.work_graph import family_ids

router = APIRouter(prefix="/discovery", tags=["discovery"])


class DiscoveryTitle(BaseModel):
    rating: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    provider: Literal["hardcover", "local"]
    external_id: str | None = None
    title: str
    authors: list[str]
    cover_url: str | None = None
    publication_year: int | None = None
    release_date: date | None = None
    genres: list[str] = []
    date_basis: Literal["audiobook", "work", "unknown"] = "unknown"


class DiscoveryItem(BaseModel):
    book: DiscoveryTitle
    work: WorkView | None = None
    reason: str


class DiscoveryShelf(BaseModel):
    title: str
    attribution: str
    status: Literal["ready", "not-connected", "unavailable"] = "ready"
    items: list[DiscoveryItem] = Field(default_factory=list)
    page: int = 1
    has_more: bool = False
    stale: bool = False
    warning: str | None = None
    retry_after: int | None = None


class LibraryDiscoveryItem(DiscoveryItem):
    observed_at: datetime


class LibraryDiscoveryShelf(DiscoveryShelf):
    items: list[LibraryDiscoveryItem] = Field(default_factory=list)


async def project(db, user, books, reason):
    """Project visible ownership from accepted links or unique exact book matches."""
    works = await displayed_provider_works(db, user, "hardcover", books)
    availability = await availability_for(db, user, list({work.id for work in works.values()}))
    result = []
    seen = set()
    for book in books:
        work = works.get(("hardcover", book.external_id))
        if work and work.id in seen:
            continue
        if work:
            seen.add(work.id)
        result.append(
            DiscoveryItem(
                book=DiscoveryTitle.model_validate(book.model_dump()),
                work=work_view(work, availability[work.id]) if work else None,
                reason=reason,
            )
        )
    return result


async def local_items(db, user, rows, reason):
    availability = await availability_for(db, user, [work.id for work in rows])
    return [
        DiscoveryItem(
            book=DiscoveryTitle(
                provider="local",
                title=work.title,
                authors=work.authors,
                cover_url=work.cover_url,
                publication_year=work.publication_year,
            ),
            work=work_view(work, availability[work.id]),
            reason=reason,
        )
        for work in rows
    ]


@router.get("/local", response_model=DiscoveryShelf)
async def local(user: CurrentUser, db: Database):
    rows = list(
        await db.scalars(
            select(Work)
            .where(Work.id.in_(display_ids(user)), visible_work(user))
            .order_by(Work.created_at.desc(), Work.id)
            .limit(20)
        )
    )
    return DiscoveryShelf(
        title="Recently added to your catalog",
        attribution="Your accessible catalog · newest additions first",
        items=await local_items(db, user, rows, "Recently added to your catalog"),
    )


@router.get("/library", response_model=LibraryDiscoveryShelf)
async def library(
    user: CurrentUser,
    db: Database,
    medium: Literal["any", "ebook", "audio"] = "any",
    page: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=12, ge=1, le=24),
):
    mapping = display_map(user)
    holdings = availability_rows(user, mapping)
    if medium != "any":
        holdings = holdings.where(LibraryAsset.medium == medium)
    recent = (
        holdings.with_only_columns(
            mapping.c.work_id, func.max(LibraryAsset.created_at).label("observed_at")
        )
        .group_by(mapping.c.work_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Work, recent.c.observed_at)
            .join(recent, recent.c.work_id == Work.id)
            .where(visible_work(user))
            .order_by(recent.c.observed_at.desc(), Work.title, Work.id)
            .offset((page - 1) * limit)
            .limit(limit + 1)
        )
    ).all()
    items = await local_items(
        db, user, [work for work, _ in rows[:limit]], "Recently observed complete library copy"
    )
    return LibraryDiscoveryShelf(
        title="Recent library additions",
        attribution="Your accessible libraries · latest copy first observed by this app",
        page=page,
        has_more=len(rows) > limit,
        items=[
            LibraryDiscoveryItem(**item.model_dump(), observed_at=observed_at)
            for item, (_, observed_at) in zip(items, rows[:limit], strict=True)
        ],
    )


@router.get("/hardcover/{shelf}", response_model=DiscoveryShelf)
async def hardcover(
    shelf: Literal["trending", "new-releases"],
    user: CurrentUser,
    db: Database,
    page: int = Query(default=1, ge=1, le=25),
):
    result = DiscoveryShelf(
        title="Trending on Hardcover" if shelf == "trending" else "New releases",
        attribution="Hardcover · trending over the last month"
        if shelf == "trending"
        else "Hardcover · published in the last 90 days, newest first",
        page=page,
    )
    account = await db.get(CatalogAccount, user.id)
    if not account or not account.enabled:
        result.status, result.warning = (
            "not-connected",
            "Connect your Hardcover account to browse this shelf.",
        )
        return result
    user_id = user.id
    try:
        batch, result.stale, result.warning = await provider_call(
            db,
            user_id,
            "hardcover",
            "discovery",
            shelf,
            page,
            datetime.now(UTC).date(),
            background=True,
        )
    except AdapterError as error:
        result.status, result.warning, result.retry_after = (
            "unavailable",
            str(error),
            error.retry_after,
        )
        return result
    user = await current_actor(db, user_id)
    result.items = await project(db, user, batch.items, result.attribution)
    result.has_more = batch.has_more
    result.warning = result.warning or batch.warning
    return result


@router.get("/related/{work_id}", response_model=DiscoveryShelf)
async def related(work_id: UUID, user: CurrentUser, db: Database):
    work = await accessible_work(db, user, work_id)
    work_id, user_id, authors = work.id, user.id, list(work.authors)
    source = await db.scalar(
        select(WorkMetadataSource)
        .join(Work, Work.id == WorkMetadataSource.work_id)
        .where(
            WorkMetadataSource.work_id.in_(family_ids(work_id)),
            WorkMetadataSource.provider == "hardcover",
            WorkMetadataSource.accepted.is_(True),
            visible_origin_work(user),
        )
        .order_by(WorkMetadataSource.fetched_at.desc(), WorkMetadataSource.id)
        .limit(1)
    )
    account = await db.get(CatalogAccount, user_id)
    result = DiscoveryShelf(title="Related books", attribution="Your catalog · shared author")
    if source and account and account.enabled:
        try:
            batch, result.stale, result.warning = await provider_call(
                db, user_id, "hardcover", "related", source.external_id, background=True
            )
            user = await current_actor(db, user_id)
            # Access can change while the provider request is in flight.
            work_id = (await accessible_work(db, user, work_id)).id
            result.items = [
                item
                for item in await project(db, user, batch.items, "Suggested by Hardcover")
                if not item.work or item.work.id != work_id
            ]
            result.warning = result.warning or batch.warning
            if result.items:
                result.attribution = "Hardcover · related-title suggestions"
                return result
        except AdapterError as error:
            result.warning = (
                f"Hardcover suggestions unavailable. {error} Showing local author matches."
            )
            result.retry_after = error.retry_after
    user = await current_actor(db, user_id)
    work = await accessible_work(db, user, work_id)
    authors = list(work.authors)
    rows = list(
        await db.scalars(
            select(Work)
            .where(
                Work.id.in_(display_ids(user)),
                Work.id != work_id,
                visible_work(user),
                or_(*(Work.authors.contains([author]) for author in authors[:10]))
                if authors
                else False,
            )
            .order_by(Work.title, Work.id)
            .limit(20)
        )
    )
    result.items = await local_items(db, user, rows, "Shares an author with this book")
    result.stale = False
    return result
