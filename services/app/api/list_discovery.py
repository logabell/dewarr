"""Local, owner-scoped followed-list shelves without remote observation side effects."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.catalog import WorkView, work_view
from app.api.dependencies import CurrentUser, Database
from app.db.models import BookList, LibraryAsset, ListEntry, ListSubscription, Work
from app.domain.availability import availability_for, availability_rows
from app.domain.catalog_display import display_map
from app.domain.visibility import visible_work

router = APIRouter(prefix="/discovery", tags=["discovery"])


class FollowedListCard(BaseModel):
    id: UUID
    name: str
    provider: Literal["hardcover", "goodreads", "storygraph"]
    enabled: bool
    sync_state: str
    last_success_at: datetime | None
    count: int
    owned: int
    provisional: int
    inventory_stale: bool
    books: list[WorkView]


class FollowedListShelf(BaseModel):
    items: list[FollowedListCard]
    total: int
    offset: int
    limit: int


@router.get("/followed-lists", response_model=FollowedListShelf)
async def followed_lists(
    user: CurrentUser,
    db: Database,
    provider: Literal["all", "hardcover", "goodreads", "storygraph"] = "all",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=4, ge=1, le=12),
):
    conditions = [
        BookList.owner_id == user.id,
        ListSubscription.provider.in_(["hardcover", "goodreads", "storygraph"]),
    ]
    if provider != "all":
        conditions.append(ListSubscription.provider == provider)
    base = (
        select(BookList, ListSubscription)
        .join(ListSubscription, ListSubscription.list_id == BookList.id)
        .where(*conditions)
    )
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await db.execute(
            base.order_by(ListSubscription.created_at.desc(), BookList.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    if not rows:
        return FollowedListShelf(items=[], total=total or 0, offset=offset, limit=limit)

    # Limit identity traversal to the visible list page, while display_map still
    # includes matching editions outside those lists for grouping and ownership.
    mapping = display_map(
        user,
        select(ListEntry.work_id).where(ListEntry.list_id.in_([item.id for item, _ in rows])),
    )
    roots = (
        select(ListEntry.list_id, mapping.c.work_id, func.min(ListEntry.position).label("position"))
        .join(mapping, mapping.c.origin_id == ListEntry.work_id)
        .join(Work, Work.id == mapping.c.work_id)
        .where(ListEntry.list_id.in_([item.id for item, _ in rows]), visible_work(user))
        .group_by(ListEntry.list_id, mapping.c.work_id)
        .subquery()
    )
    holdings = (
        availability_rows(user, mapping)
        .with_only_columns(
            mapping.c.work_id,
            func.bool_or(LibraryAsset.state == "stale").label("inventory_stale"),
        )
        .where(mapping.c.work_id.in_(select(roots.c.work_id)))
        .group_by(mapping.c.work_id)
        .subquery()
    )
    summaries = {
        row.list_id: row
        for row in (
            await db.execute(
                select(
                    roots.c.list_id,
                    func.count().label("book_count"),
                    func.count(holdings.c.work_id).label("owned"),
                    func.count().filter(Work.provisional.is_(True)).label("provisional"),
                    func.bool_or(holdings.c.inventory_stale).label("inventory_stale"),
                )
                .join(Work, Work.id == roots.c.work_id)
                .outerjoin(holdings, holdings.c.work_id == roots.c.work_id)
                .group_by(roots.c.list_id)
            )
        ).all()
    }
    ordered = select(
        roots.c.list_id,
        roots.c.work_id,
        func.row_number()
        .over(partition_by=roots.c.list_id, order_by=(roots.c.position, roots.c.work_id))
        .label("rank"),
    ).subquery()
    previews = (
        await db.execute(
            select(ordered.c.list_id, Work)
            .join(Work, Work.id == ordered.c.work_id)
            .where(ordered.c.rank <= 3)
            .order_by(ordered.c.list_id, ordered.c.rank)
        )
    ).all()
    availability = await availability_for(db, user, list({work.id for _, work in previews}))
    books = {}
    for list_id, work in previews:
        books.setdefault(list_id, []).append(work_view(work, availability[work.id]))
    return FollowedListShelf(
        total=total or 0,
        offset=offset,
        limit=limit,
        items=[
            FollowedListCard(
                id=item.id,
                name=item.name,
                provider=subscription.provider,
                enabled=subscription.enabled,
                sync_state=subscription.state,
                last_success_at=subscription.last_success_at,
                count=summaries[item.id].book_count if item.id in summaries else 0,
                owned=summaries[item.id].owned if item.id in summaries else 0,
                provisional=summaries[item.id].provisional if item.id in summaries else 0,
                inventory_stale=bool(summaries[item.id].inventory_stale)
                if item.id in summaries
                else False,
                books=books.get(item.id, []),
            )
            for item, subscription in rows
        ],
    )
