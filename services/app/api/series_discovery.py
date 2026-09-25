"""Catalog-observed series gaps, without inferring reading progress or main membership."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.adapters.contracts import AdapterError
from app.api.catalog import WorkView
from app.api.dependencies import CurrentUser, Database
from app.api.series import series_work_view
from app.db.models import CatalogAccount, CatalogSeries, SeriesGapDismissal, SeriesMembership, Work
from app.domain.availability import availability_for, availability_rows
from app.domain.pack_coverage import CATALOG_FRESH_FOR
from app.domain.series_gap_watch import (
    PROVIDER,
    dismiss,
    mark_seen,
    owned_series_refs,
    unseen_index,
)
from app.domain.visibility import visible_work
from app.domain.work_graph import canonical_map

router = APIRouter(prefix="/discovery", tags=["discovery"])
Medium = Literal["any", "ebook", "audio"]


class SeriesGapBook(BaseModel):
    work: WorkView
    position: str | None
    ambiguous_position: bool
    unseen: bool = False


class SeriesGap(BaseModel):
    external_id: str
    name: str
    fetched_at: datetime
    catalog_stale: bool
    inventory_stale: bool
    owned: int
    ebook: int
    audio: int
    published: int
    missing: int
    unknown_publication: int
    future_publication: int
    unseen: int = 0
    books: list[SeriesGapBook]


class SeriesGapShelf(BaseModel):
    items: list[SeriesGap] = Field(default_factory=list)
    medium: Medium
    page: int
    has_more: bool
    unseen: int = 0
    suggestions_enabled: bool = False
    hardcover_connected: bool = False
    monitored_series: int = 0
    attribution: str = "Your observed Hardcover catalogs and accessible library holdings"


class SeenInput(BaseModel):
    external_id: str | None = None


def normal_entry():
    data = SeriesMembership.snapshot
    return (
        data["compilation"].as_boolean().is_(False),
        data["partial"].as_boolean().is_(False),
        data["canonical_id"].as_string().is_(None),
    )


def entries_for(user, mapping):
    return (
        select(SeriesMembership.series_id)
        .join(mapping, mapping.c.origin_id == SeriesMembership.work_id)
        .join(Work, Work.id == mapping.c.work_id)
        .where(SeriesMembership.present.is_(True), visible_work(user), *normal_entry())
    )


def present(availability, medium):
    return availability.owned if medium == "any" else getattr(availability, medium)


def gap_candidates(pairs, available, medium, now):
    grouped = {}
    positions = {}
    for entry, work in pairs:
        data = entry.snapshot
        if data["compilation"] or data["partial"] or data["canonical_id"]:
            continue
        grouped.setdefault(work.id, (work, []))[1].append(data)
        if data["position"] is not None:
            positions.setdefault(Decimal(data["position"]), set()).add(work.id)
    owned = sum(available[key].owned for key in grouped)
    if not owned:
        return None
    candidates = []
    published = unknown = future = 0
    for work, records in grouped.values():
        dates = [
            date.fromisoformat(record["release_date"])
            for record in records
            if record["release_date"]
        ]
        if not dates:
            unknown += 1
            continue
        if min(dates) > now.date():
            future += 1
            continue
        published += 1
        if present(available[work.id], medium):
            continue
        known_positions = {
            Decimal(record["position"]) for record in records if record["position"] is not None
        }
        position = next(iter(known_positions)) if len(known_positions) == 1 else None
        ambiguous = len(known_positions) > 1 or any(
            len(positions[item]) > 1 for item in known_positions
        )
        candidates.append((position, work, ambiguous))
    return {
        "grouped": grouped,
        "owned": owned,
        "published": published,
        "unknown": unknown,
        "future": future,
        "candidates": candidates,
    }


def projection(series, pairs, available, medium, now, *, book_limit=3, unseen_ids=None):
    # Group original entries by canonical identity before counting or selecting gaps.
    result = gap_candidates(pairs, available, medium, now)
    if not result or not result["candidates"]:
        return None
    grouped = result["grouped"]
    candidates = result["candidates"]
    unseen_ids = unseen_ids or set()

    def gap_order(value):
        position, work, _ambiguous = value
        return (
            position is None,
            position if position is not None else Decimal(0),
            work.title.casefold(),
            str(work.id),
        )

    candidates.sort(key=gap_order)
    shown = candidates if book_limit is None else candidates[:book_limit]
    return SeriesGap(
        external_id=series.external_id,
        name=series.name,
        fetched_at=series.fetched_at,
        catalog_stale=series.fetched_at < now - CATALOG_FRESH_FOR,
        inventory_stale=any(available[key].stale for key in grouped),
        owned=result["owned"],
        ebook=sum(available[key].ebook for key in grouped),
        audio=sum(available[key].audio for key in grouped),
        published=result["published"],
        missing=len(candidates),
        unknown_publication=result["unknown"],
        future_publication=result["future"],
        unseen=sum(1 for _, work, _ in candidates if work.id in unseen_ids),
        books=[
            SeriesGapBook(
                work=series_work_view(work, available[work.id], grouped[work.id][1][0]),
                position=str(position) if position is not None else None,
                ambiguous_position=ambiguous,
                unseen=work.id in unseen_ids,
            )
            for position, work, ambiguous in shown
        ],
    )


def dismissed_ids(user):
    return select(SeriesGapDismissal.external_id).where(
        SeriesGapDismissal.user_id == user.id,
        SeriesGapDismissal.provider == PROVIDER,
    )


def eligible_series(user, seed_series, missing_series):
    return (
        select(CatalogSeries)
        .where(
            CatalogSeries.owner_id == user.id,
            CatalogSeries.provider == PROVIDER,
            CatalogSeries.fetched_at.is_not(None),
            CatalogSeries.external_id.not_in(dismissed_ids(user)),
            CatalogSeries.id.in_(seed_series),
            CatalogSeries.id.in_(missing_series),
        )
        .order_by(CatalogSeries.fetched_at.desc(), CatalogSeries.name, CatalogSeries.id)
    )


async def membership_pairs(db, user, series_ids):
    if not series_ids:
        return []
    mapping = canonical_map()
    return (
        await db.execute(
            select(SeriesMembership, Work)
            .join(mapping, mapping.c.origin_id == SeriesMembership.work_id)
            .join(Work, Work.id == mapping.c.work_id)
            .where(
                SeriesMembership.series_id.in_(series_ids),
                SeriesMembership.present.is_(True),
                visible_work(user),
            )
        )
    ).all()


async def projected(db, user, rows, medium, now, book_limit, index):
    if not rows:
        return []
    members = await membership_pairs(db, user, [row.id for row in rows])
    grouped = {}
    for member, work in members:
        grouped.setdefault(member.series_id, []).append((member, work))
    available = await availability_for(
        db, user, list({work.id for _, work in members}), identity_only=True
    )
    items = [
        projection(
            row,
            grouped.get(row.id, []),
            available,
            medium,
            now,
            book_limit=book_limit,
            unseen_ids=index.get(row.external_id, set()),
        )
        for row in rows
    ]
    return [item for item in items if item]


async def series_gap_ids(db, user, series) -> list[UUID] | None:
    """Published works missing from the library, or None when the series is not owned."""
    members = await membership_pairs(db, user, [series.id])
    available = await availability_for(
        db, user, list({work.id for _, work in members}), identity_only=True
    )
    result = gap_candidates(members, available, "any", datetime.now(UTC))
    if result is None:
        return None
    return [work.id for _, work, _ in result["candidates"]]


def shelf_context(account, monitored):
    return {
        "suggestions_enabled": bool(account and account.suggest_series_gaps),
        "hardcover_connected": bool(account and account.enabled),
        "monitored_series": monitored,
    }


@router.get("/series", response_model=SeriesGapShelf)
async def series_gaps(
    user: CurrentUser,
    db: Database,
    medium: Medium = "any",
    page: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=4, ge=1, le=12),
    full: bool = False,
):
    now = datetime.now(UTC)
    ownership = availability_rows(user, canonical_map()).subquery()
    owned = select(ownership.c.work_id).distinct()
    satisfied = owned if medium == "any" else owned.where(ownership.c.medium == medium)
    mapping = canonical_map()
    entries = entries_for(user, mapping)
    seed_series = entries.where(mapping.c.work_id.in_(owned))
    missing_series = entries.where(
        mapping.c.work_id.not_in(satisfied),
        SeriesMembership.snapshot["release_date"].as_string() <= now.date().isoformat(),
    )
    account = await db.get(CatalogAccount, user.id)
    monitored = (
        len(await owned_series_refs(db, user)) if account and account.suggest_series_gaps else 0
    )
    context = shelf_context(account, monitored)
    index = await unseen_index(db, user)
    rows = list(
        await db.scalars(
            eligible_series(user, seed_series, missing_series)
            .offset((page - 1) * limit)
            .limit(limit + 1)
        )
    )
    selected = rows[:limit]
    book_limit = None if full else 3
    items = await projected(db, user, selected, medium, now, book_limit, index)
    shown = {row.external_id for row in selected}
    extra_ids = [external_id for external_id in index if external_id not in shown]
    unseen = sum(item.unseen for item in items)
    if extra_ids:
        extra = list(
            await db.scalars(
                eligible_series(user, seed_series, missing_series).where(
                    CatalogSeries.external_id.in_(extra_ids)
                )
            )
        )
        unseen += sum(
            item.unseen for item in await projected(db, user, extra, medium, now, 3, index)
        )
    return SeriesGapShelf(
        items=items,
        medium=medium,
        page=page,
        has_more=len(rows) > limit,
        unseen=unseen,
        **context,
    )


@router.post("/series/seen", status_code=204)
async def see_series_gaps(body: SeenInput, user: CurrentUser, db: Database):
    try:
        await mark_seen(db, user, body.external_id)
    except AdapterError as error:
        raise HTTPException(422, "Invalid series identifier") from error
    await db.commit()


@router.post("/series/{external_id}/dismiss", status_code=204)
async def dismiss_series_gaps(external_id: str, user: CurrentUser, db: Database):
    try:
        await dismiss(db, user, external_id)
    except AdapterError as error:
        raise HTTPException(422, "Invalid series identifier") from error
    await db.commit()
