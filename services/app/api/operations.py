from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Text, case, cast, func, or_, select

from app.api.dependencies import Admin, CurrentUser, Database
from app.config import get_settings
from app.db.models import BookList, Operation, Work
from app.domain.operations import transaction_lock
from app.domain.visibility import visible_work
from app.domain.work_graph import canonical_map
from app.jobs.queue import enqueue

router = APIRouter(tags=["operations"])


class OperationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    status: str
    message: str
    created_at: datetime
    updated_at: datetime


class ActivityContext(BaseModel):
    href: str
    label: str


class ActivityItem(OperationView):
    context: ActivityContext | None = None


class ActivityPage(BaseModel):
    items: list[ActivityItem]
    total: int
    offset: int
    limit: int
    statuses: list[str]
    kinds: list[str]


# Only known operation contracts supply navigation. Never reflect a payload URL.
LIST_CONTEXTS = {
    "lists.sync": ("list_id",),
    "lists.csv": ("list_id",),
    "lists.writeback": ("list_id",),
    "lists.writeback.compare": ("binding", "list_id"),
    "lists.requests": ("command", "list_id"),
    "lists.curate": ("command", "list_id"),
    "discovery.follow-list": ("result", "list_id"),
}
WORK_CONTEXTS = {
    "acquisition.quick-add": ("command", "work_id"),
    "metadata.enrich": ("work_id",),
    "sources.search": ("command", "work_id"),
}


def context_id(value):
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def context_reference(contracts):
    choices = []
    for kind, path in contracts.items():
        value = Operation.payload
        for key in path:
            value = value[key]
        choices.append((Operation.kind == kind, value.astext))
    return case(*choices)


async def activity_contexts(db, user, rows):
    list_ids = {row.id: context_id(row.list_reference) for row in rows if row.kind in LIST_CONTEXTS}
    work_ids = {row.id: context_id(row.work_reference) for row in rows if row.kind in WORK_CONTEXTS}
    lists = (
        dict(
            (
                await db.execute(
                    select(BookList.id, BookList.name).where(
                        BookList.id.in_([value for value in list_ids.values() if value]),
                        or_(BookList.owner_id == user.id, BookList.shared.is_(True)),
                    )
                )
            ).all()
        )
        if any(list_ids.values())
        else {}
    )
    mapping = canonical_map()
    works = (
        {
            origin: (identifier, title)
            for origin, identifier, title in (
                await db.execute(
                    select(mapping.c.origin_id, Work.id, Work.title)
                    .join(Work, Work.id == mapping.c.work_id)
                    .where(
                        mapping.c.origin_id.in_([value for value in work_ids.values() if value]),
                        visible_work(user),
                    )
                )
            ).all()
        }
        if any(work_ids.values())
        else {}
    )
    contexts = {}
    for row in rows:
        list_id, work_id = list_ids.get(row.id), work_ids.get(row.id)
        if list_id in lists:
            contexts[row.id] = ActivityContext(
                href=f"/lists/{list_id}", label=f"Open list: {lists[list_id]}"
            )
        elif work_id in works:
            identifier, title = works[work_id]
            tab = "?tab=sources" if row.kind == "sources.search" else ""
            contexts[row.id] = ActivityContext(
                href=f"/books/{identifier}{tab}", label=f"Open book: {title}"
            )
        elif row.kind == "library.sync" and user.role == "admin":
            if row.status == "completed" and row.review_total:
                contexts[row.id] = ActivityContext(href="/review", label="Review library items")
            else:
                contexts[row.id] = ActivityContext(
                    href="/connections", label="Review library connections"
                )
    return contexts


@router.get("/activity/page", response_model=ActivityPage)
async def activity_page(
    user: CurrentUser,
    db: Database,
    q: str = Query(default="", max_length=300),
    status: str = Query(default="", max_length=40),
    kind: str = Query(default="", max_length=60),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    where = [Operation.owner_id == user.id]
    if status:
        where.append(Operation.status == status)
    if kind:
        where.append(Operation.kind == kind)
    if q.strip():
        pattern = (
            "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        where.append(
            or_(
                Operation.message.ilike(pattern),
                Operation.kind.ilike(pattern),
                cast(Operation.id, Text).ilike(pattern),
            )
        )
    total = await db.scalar(select(func.count()).select_from(Operation).where(*where))
    rows = (
        await db.execute(
            select(
                Operation.id,
                Operation.kind,
                Operation.status,
                Operation.message,
                Operation.created_at,
                Operation.updated_at,
                context_reference(LIST_CONTEXTS).label("list_reference"),
                context_reference(WORK_CONTEXTS).label("work_reference"),
                Operation.payload["review"]["total"].as_integer().label("review_total"),
            )
            .where(*where)
            .order_by(Operation.created_at.desc(), Operation.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    facets = (
        await db.execute(
            select(Operation.status, Operation.kind).where(Operation.owner_id == user.id).distinct()
        )
    ).all()
    contexts = await activity_contexts(db, user, rows)
    return ActivityPage(
        items=[
            ActivityItem(
                **OperationView.model_validate(row).model_dump(), context=contexts.get(row.id)
            )
            for row in rows
        ],
        total=total or 0,
        offset=offset,
        limit=limit,
        statuses=sorted({state for state, _ in facets}),
        kinds=sorted({category for _, category in facets}),
    )


@router.get("/activity", response_model=list[OperationView])
async def activity(user: CurrentUser, db: Database):
    return (
        await db.scalars(
            select(Operation)
            .where(Operation.owner_id == user.id)
            .order_by(Operation.created_at.desc())
            .limit(100)
        )
    ).all()


@router.post("/system/probe", response_model=OperationView, status_code=202)
async def probe(
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Dispatch is paused for recovery")
    await transaction_lock(db, f"operation:{admin.id}:{idempotency_key}")
    existing = await db.scalar(
        select(Operation).where(
            Operation.owner_id == admin.id,
            Operation.idempotency_key == idempotency_key,
        )
    )
    if existing:
        if existing.kind != "system.probe":
            raise HTTPException(409, "This operation key was already used for another command")
        return existing
    operation = Operation(
        owner_id=admin.id,
        kind="system.probe",
        idempotency_key=idempotency_key,
    )
    db.add(operation)
    await db.flush()
    operation.job_id = await enqueue(db, "system.probe", operation_id=str(operation.id))
    await db.commit()
    await db.refresh(operation)
    return operation
