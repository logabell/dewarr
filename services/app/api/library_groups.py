"""Collection-only author and series shelves, grouped before pagination."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import Column, Text, case, cast, func, literal, select, true, union_all
from sqlalchemy.dialects.postgresql import JSONB

from app.api.catalog import WorkPage, WorkView, work_view
from app.api.dependencies import CurrentUser, Database
from app.db.models import CatalogSeries, LibraryAsset, SeriesMembership, Work, WorkMetadataSource
from app.domain.availability import availability_for, availability_rows, library_work_ids
from app.domain.catalog_display import display_map
from app.domain.visibility import visible_origin_work

router = APIRouter()
Kind = Literal["authors", "series"]
Medium = Literal["any", "ebook", "audio"]


class LibraryGroup(BaseModel):
    name: str
    key: str
    book_count: int
    external_id: str | None = None
    books: list[WorkView]
    hardcover_book_id: str | None = None


class LibraryGroupPage(BaseModel):
    items: list[LibraryGroup]
    total: int
    offset: int
    limit: int


def normalized(value):
    return func.lower(func.trim(func.regexp_replace(value, r"\s+", " ", "g")))


def array(value):
    return case((func.jsonb_typeof(value) == "array", value), else_=cast(literal("[]"), JSONB))


def group_rows(user, kind, medium, library_id):
    mapping = display_map(user, library_work_ids(user, library_id=library_id, medium=medium))
    holdings = availability_rows(user, mapping)
    if medium != "any":
        holdings = holdings.where(LibraryAsset.medium == medium)
    if library_id:
        holdings = holdings.where(LibraryAsset.library_id == library_id)
    owned = holdings.with_only_columns(mapping.c.work_id).distinct().subquery()
    if kind == "authors":
        names = func.jsonb_array_elements_text(Work.authors).table_valued("value")
        raw = (
            select(
                Work.id.label("work_id"),
                names.c.value.label("name"),
                cast(literal(None), Text).label("external_id"),
            )
            .join(owned, owned.c.work_id == Work.id)
            .join(names, true())
        ).subquery()
    else:
        local = holdings.with_only_columns(
            mapping.c.work_id, LibraryAsset.metadata_snapshot.label("snapshot")
        ).subquery()
        local_names = func.jsonb_array_elements(array(local.c.snapshot["series"])).table_valued(
            Column("value", JSONB)
        )
        local_series = (
            select(
                local.c.work_id,
                local_names.c.value["name"].astext.label("name"),
                cast(literal(None), Text).label("external_id"),
            )
            .select_from(local)
            .join(local_names, true())
        )
        source_names = func.jsonb_array_elements(
            array(WorkMetadataSource.snapshot["series"])
        ).table_valued(Column("value", JSONB))
        metadata_series = (
            select(
                mapping.c.work_id,
                source_names.c.value["name"].astext.label("name"),
                case(
                    (
                        WorkMetadataSource.provider == "hardcover",
                        source_names.c.value["external_id"].astext,
                    ),
                    else_=None,
                ).label("external_id"),
            )
            .select_from(WorkMetadataSource)
            .join(Work, Work.id == WorkMetadataSource.work_id)
            .join(mapping, mapping.c.origin_id == WorkMetadataSource.work_id)
            .join(owned, owned.c.work_id == mapping.c.work_id)
            .join(source_names, true())
            .where(WorkMetadataSource.accepted.is_(True), visible_origin_work(user))
        )
        observed_series = (
            select(
                mapping.c.work_id,
                CatalogSeries.name,
                case(
                    (CatalogSeries.provider == "hardcover", CatalogSeries.external_id), else_=None
                ).label("external_id"),
            )
            .select_from(SeriesMembership)
            .join(CatalogSeries)
            .join(mapping, mapping.c.origin_id == SeriesMembership.work_id)
            .join(owned, owned.c.work_id == mapping.c.work_id)
            .where(CatalogSeries.owner_id == user.id, SeriesMembership.present.is_(True))
        )
        raw = union_all(local_series, metadata_series, observed_series).subquery()
    return (
        select(
            raw.c.work_id,
            func.trim(raw.c.name).label("name"),
            normalized(raw.c.name).label("key"),
            raw.c.external_id,
        )
        .where(normalized(raw.c.name) != "")
        .distinct()
        .subquery()
    )


@router.get("/groups/{kind}", response_model=LibraryGroupPage)
async def groups(
    kind: Kind,
    user: CurrentUser,
    db: Database,
    q: str = Query(default="", max_length=300),
    medium: Medium = "any",
    library_id: UUID | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=24, ge=1, le=100),
):
    rows = group_rows(user, kind, medium, library_id)
    grouped = select(
        rows.c.key,
        func.min(rows.c.name).label("name"),
        func.count(func.distinct(rows.c.work_id)).label("book_count"),
        case(
            (func.count(func.distinct(rows.c.external_id)) == 1, func.min(rows.c.external_id)),
            else_=None,
        ).label("external_id"),
    ).group_by(rows.c.key)
    if q.strip():
        grouped = grouped.where(rows.c.key.contains(" ".join(q.lower().split()), autoescape=True))
    grouped = grouped.subquery()
    total = await db.scalar(select(func.count()).select_from(grouped))
    selected = (
        await db.execute(select(grouped).order_by(grouped.c.key).offset(offset).limit(limit))
    ).all()
    pairs = (
        select(rows.c.key, rows.c.work_id)
        .where(rows.c.key.in_([row.key for row in selected]))
        .distinct()
        .subquery()
    )
    ranked = (
        select(
            pairs.c.key,
            pairs.c.work_id,
            func.row_number()
            .over(partition_by=pairs.c.key, order_by=[Work.title, Work.id])
            .label("rank"),
        )
        .join(Work, Work.id == pairs.c.work_id)
        .subquery()
    )
    samples = (
        (
            await db.execute(
                select(ranked.c.key, Work)
                .join(Work, Work.id == ranked.c.work_id)
                .where(ranked.c.rank <= 3)
                .order_by(ranked.c.key, ranked.c.rank)
            )
        ).all()
        if selected
        else []
    )
    availability = await availability_for(db, user, list({work.id for _, work in samples}))
    by_group = {}
    for key, work in samples:
        by_group.setdefault(key, []).append(work_view(work, availability[work.id]))
    # Resolve accepted identities in one query, including consolidated editions.
    mapping = display_map(user, select(pairs.c.work_id))
    portrait_books = (
        dict(
            (
                await db.execute(
                    select(pairs.c.key, func.min(WorkMetadataSource.external_id))
                    .select_from(pairs)
                    .join(mapping, mapping.c.work_id == pairs.c.work_id)
                    .join(Work, Work.id == mapping.c.origin_id)
                    .join(WorkMetadataSource, WorkMetadataSource.work_id == Work.id)
                    .where(
                        WorkMetadataSource.provider == "hardcover",
                        WorkMetadataSource.accepted.is_(True),
                        visible_origin_work(user),
                    )
                    .group_by(pairs.c.key)
                )
            ).all()
        )
        if selected
        else {}
    )
    return LibraryGroupPage(
        items=[
            LibraryGroup(
                **row._asdict(),
                books=by_group.get(row.key, []),
                hardcover_book_id=portrait_books.get(row.key),
            )
            for row in selected
        ],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


@router.get("/groups/{kind}/books", response_model=WorkPage)
async def group_books(
    kind: Kind,
    user: CurrentUser,
    db: Database,
    name: str = Query(min_length=1, max_length=600),
    medium: Medium = "any",
    library_id: UUID | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=40, ge=1, le=100),
):
    rows = group_rows(user, kind, medium, library_id)
    ids = select(rows.c.work_id).where(rows.c.key == " ".join(name.lower().split())).distinct()
    query = select(Work).where(Work.id.in_(ids))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    books = list(await db.scalars(query.order_by(Work.title, Work.id).offset(offset).limit(limit)))
    availability = await availability_for(db, user, [work.id for work in books])
    return WorkPage(
        items=[work_view(work, availability[work.id]) for work in books],
        total=total or 0,
        offset=offset,
        limit=limit,
    )
