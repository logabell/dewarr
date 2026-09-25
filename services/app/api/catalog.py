from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.api.dependencies import CurrentUser, Database, Member
from app.db.models import AuditEvent, Work
from app.domain.availability import Availability, availability_for
from app.domain.catalog_display import display_map
from app.domain.catalog_search import local_match
from app.domain.identity import work_key
from app.domain.visibility import visible_origin_work, visible_work
from app.domain.work_graph import canonical_work

router = APIRouter(prefix="/catalog", tags=["catalog"])


class WorkInput(BaseModel):
    title: str = Field(min_length=1, max_length=600)
    authors: list[str] = Field(default_factory=list, max_length=30)
    description: str | None = Field(default=None, max_length=30000)
    language: str | None = Field(default=None, max_length=20)
    publication_year: int | None = Field(default=None, ge=0, le=9999)

    @field_validator("title")
    @classmethod
    def title_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a title")
        return value.strip()

    @field_validator("authors")
    @classmethod
    def validate_authors(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("Each author must contain 1–300 characters")
        return [value.strip() for value in values]


class WorkView(BaseModel):
    id: UUID
    title: str
    authors: list[str]
    description: str | None
    language: str | None
    publication_year: int | None
    cover_url: str | None
    provisional: bool
    availability: Availability


class WorkPage(BaseModel):
    items: list[WorkView]
    total: int
    offset: int
    limit: int


def work_view(work: Work, availability: Availability) -> WorkView:
    return WorkView(
        id=work.id,
        title=work.title,
        authors=work.authors,
        description=work.description,
        language=work.language,
        publication_year=work.publication_year,
        cover_url=work.cover_url,
        provisional=work.provisional,
        availability=availability,
    )


@router.get("/works", response_model=WorkPage)
async def works(
    user: CurrentUser,
    db: Database,
    q: str = Query(default="", max_length=300),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=40, ge=1, le=100),
    medium: Literal["any", "ebook", "audio"] = "any",
):
    mapping = display_map(user)
    conditions = [Work.id.in_(select(mapping.c.work_id).distinct()), visible_work(user)]
    if medium != "any":
        from app.db.models import LibraryAsset
        from app.domain.availability import availability_rows

        conditions.append(
            Work.id.in_(
                availability_rows(user, mapping)
                .with_only_columns(mapping.c.work_id)
                .where(LibraryAsset.medium == medium)
            )
        )
    if q.strip():
        from sqlalchemy.orm import aliased

        origin = aliased(Work)
        conditions.append(
            Work.id.in_(
                select(mapping.c.work_id)
                .join(origin, origin.id == mapping.c.origin_id)
                .where(
                    local_match(user, origin, q),
                    visible_origin_work(user, origin),
                )
            )
        )
    # Count grouped identities once, keeping large metadata outside the window.
    page = (
        select(Work.id, func.count().over().label("total"))
        .where(*conditions)
        .order_by(Work.title, Work.id)
        .offset(offset)
        .limit(limit)
        .subquery()
    )
    records = (
        await db.execute(
            select(Work, page.c.total)
            .join(page, page.c.id == Work.id)
            .order_by(Work.title, Work.id)
        )
    ).all()
    rows = [work for work, _ in records]
    total = (
        records[0][1]
        if records
        else (
            await db.scalar(select(func.count()).select_from(Work).where(*conditions))
            if offset
            else 0
        )
    )
    availability = await availability_for(db, user, [work.id for work in rows])
    return WorkPage(
        items=[work_view(work, availability[work.id]) for work in rows],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.post("/works", response_model=WorkView, status_code=201)
async def add_work(body: WorkInput, user: Member, db: Database):
    work = Work(**body.model_dump())
    work.metadata_fields = {
        "fields": {
            field: {
                "value": value,
                "provider": "manual",
                "locked": True,
                "reason": "Entered by user",
            }
            for field, value in body.model_dump(exclude_unset=True).items()
        }
    }
    work.match_key = work_key(work.title, work.authors)
    db.add(work)
    await db.flush()
    db.add(AuditEvent(actor_id=user.id, action="catalog.work.created", entity_id=work.id))
    await db.commit()
    return work_view(work, Availability())


@router.get("/works/{work_id}", response_model=WorkView)
async def work_detail(work_id: UUID, user: CurrentUser, db: Database):
    canonical = await canonical_work(db, work_id)
    # Display grouping chooses cards and covers, not the identity of a bookmark.
    work = await db.scalar(select(Work).where(Work.id == canonical.id, visible_work(user)))
    if not work:
        raise HTTPException(404, "Book not found")
    availability = await availability_for(db, user, [work.id])
    return work_view(work, availability[work.id])


@router.get("/works/{work_id}/cover", response_class=Response)
async def cover(
    work_id: UUID,
    user: CurrentUser,
    db: Database,
    medium: Literal["ebook", "audio"] = "ebook",
    if_none_match: str | None = Header(default=None),
):
    from app.domain.library_covers import library_cover

    return await library_cover(db, user, work_id, medium, if_none_match)


@router.get("/cover-image", response_class=Response)
async def cover_image(user: CurrentUser, db: Database, url: str = Query(max_length=2000)):
    from app.domain.cover_cache import cached_cover

    return await cached_cover(db, url)
