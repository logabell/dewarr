"""Read-only, canonical follow summaries from verified local observations.

No provider calls or acquisition side effects. Counts are computed before paging;
book snapshots and availability are projected in bounded batches.
"""

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select

from app.adapters.catalog_types import cover_url
from app.db.models import BookList, ListAcquisitionPolicy, ListObservation, ListSubscription
from app.domain.availability import availability_rows
from app.domain.work_graph import canonical_map
from app.security import decrypt_secrets

BookFilter = Literal["all", "library", "upcoming", "recent", "missing"]


class FollowBook(BaseModel):
    work_id: UUID
    external_id: str
    title: str
    authors: list[str]
    cover_url: str | None = None
    release_date: date | None = None
    upcoming: bool = False
    included: bool = True
    ebook: bool = False
    audio: bool = False
    stale: bool = False
    follow_names: list[str] = Field(default_factory=list)


class FollowBooks(BaseModel):
    items: list[FollowBook]
    total: int
    offset: int
    limit: int


class FollowSummary(BaseModel):
    list_id: UUID
    source_kind: Literal["author", "series"]
    external_id: str
    name: str
    image_url: str | None = None
    followed_at: datetime
    enabled: bool
    state: str
    message: str
    complete: bool
    last_success_at: datetime | None
    mode: str
    active: bool
    total_books: int | None = None
    library_books: int | None = None
    upcoming_books: int | None = None
    undated_books: int | None = None
    recent_books: int | None = None
    missing_books: int | None = None
    next_release: FollowBook | None = None
    latest_books: list[FollowBook] = Field(default_factory=list)


class FollowOverview(BaseModel):
    items: list[FollowSummary]
    total: int
    authors: int
    series: int
    pending_sync: bool
    catalog_revision: str
    offset: int
    limit: int


async def sources(db, user):
    rows = (
        await db.execute(
            select(BookList, ListSubscription, ListAcquisitionPolicy)
            .join(ListSubscription, ListSubscription.list_id == BookList.id)
            .outerjoin(ListAcquisitionPolicy, ListAcquisitionPolicy.list_id == BookList.id)
            .where(
                BookList.owner_id == user.id,
                ListSubscription.provider == "hardcover",
                ListSubscription.source_kind.in_(["author", "series"]),
            )
        )
    ).all()
    result = []
    for item, row, policy in rows:
        config = decrypt_secrets(row.encrypted_config)
        if config.get("unfollowed"):
            continue
        result.append(
            (
                row,
                FollowSummary(
                    list_id=item.id,
                    source_kind=row.source_kind,
                    external_id=config["external_id"],
                    name=config.get("name") or item.name,
                    image_url=cover_url(config.get("image_url")),
                    followed_at=config.get("followed_at") or row.created_at,
                    enabled=row.enabled,
                    state=row.state,
                    message=row.message,
                    complete=bool(config.get("complete")),
                    last_success_at=row.last_success_at,
                    mode=policy.configuration["mode"] if policy else "browse",
                    active=bool(policy and policy.active),
                ),
            )
        )
    return result


def catalog(user, source_rows):
    mapping = canonical_map()
    availability = availability_rows(user, mapping)
    holdings = (
        availability.with_only_columns(
            mapping.c.work_id,
            func.bool_or(availability.selected_columns.medium == "ebook").label("ebook"),
            func.bool_or(availability.selected_columns.medium == "audio").label("audio"),
            func.bool_and(availability.selected_columns.state == "stale").label("stale"),
        )
        .group_by(mapping.c.work_id)
        .subquery()
    )
    # Snapshots are only replaced when the complete two-pass observation commits.
    # Explicit exclusions win when provider aliases resolve to the same work.
    rows = (
        select(
            ListSubscription.list_id,
            mapping.c.work_id,
            ListObservation.external_id,
            ListObservation.snapshot,
            ListObservation.excluded,
            ListObservation.last_seen_at.label("observed_at"),
            func.coalesce(holdings.c.ebook, False).label("ebook"),
            func.coalesce(holdings.c.audio, False).label("audio"),
            func.coalesce(holdings.c.stale, False).label("stale"),
            func.row_number()
            .over(
                partition_by=(ListSubscription.list_id, mapping.c.work_id),
                order_by=(
                    ListObservation.excluded.desc(),
                    ListObservation.snapshot["filter_reason"].astext.asc().nullsfirst(),
                    ListObservation.last_seen_at.desc(),
                    ListObservation.external_id,
                ),
            )
            .label("position"),
        )
        .select_from(ListObservation)
        .join(ListSubscription, ListSubscription.id == ListObservation.subscription_id)
        .join(mapping, mapping.c.origin_id == ListObservation.work_id)
        .outerjoin(holdings, holdings.c.work_id == mapping.c.work_id)
        .where(
            ListObservation.subscription_id.in_([row.id for row, _ in source_rows]),
            ListObservation.present.is_(True),
        )
        .subquery()
    )
    day = rows.c.snapshot["release_date"].astext
    today = datetime.now(UTC).date()
    eligible = and_(rows.c.excluded.is_(False), rows.c.snapshot["filter_reason"].astext.is_(None))
    upcoming = func.coalesce(
        (day > today.isoformat())
        | (day.is_(None) & (rows.c.snapshot["coming_soon"].astext == "true")),
        False,
    )
    released = func.coalesce(day <= today.isoformat(), False)
    return (
        select(
            rows,
            day.label("day"),
            eligible.label("eligible"),
            upcoming.label("upcoming"),
            released.label("released"),
            (rows.c.ebook | rows.c.audio).label("owned"),
            (released & (day >= (today - timedelta(days=90)).isoformat())).label("recent"),
        )
        .where(rows.c.position == 1)
        .subquery()
    )


def book_filter(c, selection):
    if selection == "library":
        return c.c.owned
    if selection == "upcoming":
        return c.c.eligible & c.c.upcoming
    if selection == "recent":
        return c.c.eligible & c.c.recent
    if selection == "missing":
        return c.c.eligible & c.c.released & ~c.c.owned
    return True


def book(row, names=()):
    return FollowBook(
        work_id=row.work_id,
        external_id=row.external_id,
        title=row.snapshot["title"],
        authors=row.snapshot.get("authors", []),
        cover_url=cover_url(row.snapshot.get("cover_url")),
        release_date=row.day,
        upcoming=row.upcoming,
        included=row.eligible,
        ebook=row.ebook,
        audio=row.audio,
        stale=row.stale,
        follow_names=list(names),
    )


async def summaries(db, user, rows, *, kind, q, filter, sort, offset, limit):
    authors = sum(summary.source_kind == "author" for _, summary in rows)
    series = len(rows) - authors
    # Global status must survive filtering, pagination, and switching to Releases.
    pending_sync = any(s.enabled and s.state in {"queued", "running"} for _, s in rows)
    catalog_revision = hashlib.sha256(
        "|".join(sorted(f"{s.list_id}:{s.last_success_at}" for _, s in rows)).encode()
    ).hexdigest()
    c = catalog(user, rows)
    counts = (
        (
            await db.execute(
                select(
                    c.c.list_id,
                    func.count().label("total"),
                    func.count().filter(c.c.owned).label("owned"),
                    func.count().filter(c.c.eligible & c.c.upcoming).label("upcoming"),
                    func.count()
                    .filter(c.c.eligible & c.c.upcoming & c.c.day.is_(None))
                    .label("undated"),
                    func.count().filter(c.c.eligible & c.c.recent).label("recent"),
                    func.count().filter(c.c.eligible & c.c.released & ~c.c.owned).label("missing"),
                    func.min(c.c.day).filter(c.c.eligible & c.c.upcoming).label("next_day"),
                ).group_by(c.c.list_id)
            )
        ).all()
        if rows
        else []
    )
    by_id = {r.list_id: r for r in counts}
    selected = []
    for _, item in rows:
        count = by_id.get(item.list_id)
        if item.last_success_at:
            for field, attr in (
                ("total_books", "total"),
                ("library_books", "owned"),
                ("upcoming_books", "upcoming"),
                ("undated_books", "undated"),
                ("recent_books", "recent"),
                ("missing_books", "missing"),
            ):
                setattr(item, field, getattr(count, attr) if count else 0)
        if item.source_kind != kind or q.casefold() not in item.name.casefold():
            continue
        if filter == "paused" and item.enabled:
            continue
        if filter in {"upcoming", "recent", "missing"} and not getattr(item, f"{filter}_books"):
            continue
        selected.append(item)
    if sort == "name":
        selected.sort(key=lambda s: (s.name.casefold(), s.list_id))
    elif sort == "library":
        selected.sort(key=lambda s: (-(s.library_books or 0), s.name.casefold(), s.list_id))
    elif sort == "release":
        selected.sort(
            key=lambda s: (
                (by_id[s.list_id].next_day or "9999") if s.list_id in by_id else "9999",
                s.name.casefold(),
                s.list_id,
            )
        )
    else:
        selected.sort(key=lambda s: (s.followed_at, s.list_id), reverse=True)
    total = len(selected)
    selected = selected[offset : offset + limit]
    if selected:
        for upcoming, maximum in ((True, 1), (False, 2)):
            candidates = (
                select(
                    c,
                    func.row_number()
                    .over(
                        partition_by=c.c.list_id,
                        order_by=(
                            c.c.day.asc().nullslast() if upcoming else c.c.day.desc(),
                            c.c.work_id,
                        ),
                    )
                    .label("rank"),
                )
                .where(
                    c.c.list_id.in_([s.list_id for s in selected]),
                    c.c.eligible,
                    c.c.upcoming if upcoming else c.c.released,
                )
                .subquery()
            )
            previews = (
                await db.execute(select(candidates).where(candidates.c.rank <= maximum))
            ).all()
            items = {s.list_id: s for s in selected}
            for value in sorted(previews, key=lambda r: r.rank):
                if upcoming:
                    items[value.list_id].next_release = book(value)
                else:
                    items[value.list_id].latest_books.append(book(value))
    return FollowOverview(
        items=selected,
        total=total,
        authors=authors,
        series=series,
        pending_sync=pending_sync,
        catalog_revision=catalog_revision,
        offset=offset,
        limit=limit,
    )


async def books(db, user, rows, *, selection, offset, limit, releases=False):
    if not rows:
        return FollowBooks(items=[], total=0, offset=offset, limit=limit)
    c = catalog(user, rows)
    selected = c
    names = {}
    if releases:
        # Attribution is collected separately for only the returned page below.
        # Resolve conflicting saved snapshots before classifying a release, so
        # a rescheduled book cannot appear in both Upcoming and Recent.
        ranked = (
            select(
                c,
                func.row_number()
                .over(
                    partition_by=c.c.work_id,
                    order_by=(c.c.observed_at.desc(), c.c.list_id),
                )
                .label("rank"),
            )
            .where(c.c.eligible)
            .subquery()
        )
        selected = select(ranked).where(ranked.c.rank == 1).subquery()
    selected = select(selected).where(book_filter(selected, selection)).subquery()
    total = await db.scalar(select(func.count()).select_from(selected))
    order = (
        selected.c.day.asc().nullslast()
        if selection == "upcoming"
        else selected.c.day.desc().nullslast()
    )
    records = (
        await db.execute(
            select(selected).order_by(order, selected.c.work_id).offset(offset).limit(limit)
        )
    ).all()
    if releases and records:
        labels = {s.list_id: s.name for _, s in rows}
        for work_id, list_id in (
            await db.execute(
                select(c.c.work_id, c.c.list_id).where(
                    c.c.work_id.in_([r.work_id for r in records]), c.c.eligible
                )
            )
        ).all():
            names.setdefault(work_id, set()).add(labels[list_id])
    return FollowBooks(
        items=[book(r, sorted(names.get(r.work_id, []))) for r in records],
        total=total,
        offset=offset,
        limit=limit,
    )
