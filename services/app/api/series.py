from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.adapters.contracts import AdapterError
from app.api.catalog import WorkView, work_view
from app.api.dependencies import CurrentUser, Database, Member
from app.api.metadata import adapter_http_error
from app.api.operations import OperationView
from app.db.models import CatalogSeries, MonitoredRelease, Operation, SeriesMembership, Work
from app.domain import catalog_series, series_requests, series_scopes
from app.domain.availability import availability_for
from app.domain.series_scopes import ScopeReviewInput, ScopeReviewView
from app.domain.visibility import visible_work
from app.domain.work_graph import canonical_map

router = APIRouter(prefix="/catalog/series", tags=["series"])


@router.get("/hardcover/{external_id}/main-books", response_model=ScopeReviewView)
async def main_books(external_id: str, user: Member, db: Database):
    series, user = await series_requests.context(db, user.id, external_id)
    return await series_scopes.view(db, user, series, await series_scopes.latest(db, user, series))


@router.post("/hardcover/{external_id}/main-books", response_model=ScopeReviewView, status_code=201)
async def review_main_books(
    external_id: str,
    body: ScopeReviewInput,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    series, user, review = await series_scopes.save(db, user, external_id, body, idempotency_key)
    response = await series_scopes.view(db, user, series, review)
    await db.commit()
    return response


@router.delete("/hardcover/{external_id}/main-books/{review_id}", response_model=ScopeReviewView)
async def withdraw_main_books(external_id: str, review_id: UUID, user: Member, db: Database):
    series, user, review = await series_scopes.withdraw(db, user, external_id, review_id)
    response = await series_scopes.view(db, user, series, review)
    await db.commit()
    return response


class SeriesEntryView(BaseModel):
    membership_id: UUID
    external_id: str
    position: str | None
    details: str | None
    compilation: bool
    partial: bool
    merged_record: bool
    ambiguous_position: bool
    release_date: str | None
    publication: str
    followed: bool = False
    work: WorkView


class SeriesView(BaseModel):
    id: UUID | None = None
    provider: str = "hardcover"
    external_id: str
    name: str
    description: str | None = None
    generation: int = 0
    fetched_at: datetime | None = None
    status: str
    message: str
    items: list[SeriesEntryView] = []
    total: int = 0
    offset: int
    limit: int
    books: int = 0
    owned: int = 0
    ebook: int = 0
    audio: int = 0


def series_work_view(work, availability, snapshot):
    """Display the observed member, retaining local identity and library status."""
    view = work_view(work, availability)
    book = snapshot.get("book") or {}
    # Saved observations already contain this metadata; no per-book API hydration.
    return view.model_copy(
        update={
            "title": book.get("title") or view.title,
            "authors": book.get("authors") or view.authors,
            "cover_url": book.get("cover_url") or view.cover_url,
            "publication_year": book.get("publication_year") or view.publication_year,
        }
    )


@router.post("/hardcover/{external_id}/refresh", response_model=OperationView, status_code=202)
async def refresh(
    external_id: str,
    user: Member,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    try:
        operation = await catalog_series.start(
            db, user, external_id, idempotency_key, reuse_active=True
        )
    except AdapterError as error:
        raise adapter_http_error(error) from error
    await db.commit()
    await db.refresh(operation)
    return operation


@router.get("/hardcover/{external_id}", response_model=SeriesView)
async def detail(
    external_id: str,
    user: CurrentUser,
    db: Database,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    try:
        catalog_series.identifier("hardcover", external_id)
    except AdapterError:
        raise HTTPException(422, "Invalid series identifier") from None
    row = await db.scalar(
        select(CatalogSeries).where(
            CatalogSeries.owner_id == user.id,
            CatalogSeries.provider == "hardcover",
            CatalogSeries.external_id == external_id,
        )
    )
    if not row:
        return SeriesView(
            external_id=external_id,
            name="Series",
            status="not-loaded",
            message="Load this series from your Hardcover account",
            offset=offset,
            limit=limit,
        )
    operation = await db.get(Operation, row.operation_id) if row.operation_id else None
    status, message = await catalog_series.operation_status(db, operation)
    mapping = canonical_map()
    entries = (
        await db.execute(
            select(SeriesMembership, Work)
            .join(mapping, mapping.c.origin_id == SeriesMembership.work_id)
            .join(Work, Work.id == mapping.c.work_id)
            .where(
                SeriesMembership.series_id == row.id,
                SeriesMembership.present.is_(True),
                visible_work(user),
            )
        )
    ).all()

    def order(pair):
        entry, work = pair
        pos = entry.snapshot["position"]
        return (
            pos is None,
            Decimal(pos) if pos is not None else Decimal(0),
            (entry.snapshot.get("book", {}).get("title") or work.title).casefold(),
            entry.external_id,
        )

    entries = sorted(entries, key=order)
    by_position = {}
    by_work = {}
    for entry, work in entries:
        if (
            entry.snapshot["position"] is not None
            and not entry.snapshot["compilation"]
            and not entry.snapshot["partial"]
            and not entry.snapshot["canonical_id"]
        ):
            by_position.setdefault(Decimal(entry.snapshot["position"]), set()).add(work.id)
            by_work.setdefault(work.id, set()).add(Decimal(entry.snapshot["position"]))
    available = await availability_for(
        db, user, list({work.id for _, work in entries}), identity_only=True
    )
    page = entries[offset : offset + limit]
    followed_ids = (
        set(
            await db.scalars(
                select(MonitoredRelease.work_id).where(
                    MonitoredRelease.owner_id == user.id,
                    MonitoredRelease.work_id.in_([work.id for _, work in page]),
                    MonitoredRelease.state != "stopped",
                )
            )
        )
        if page
        else set()
    )
    counted = {
        work.id
        for entry, work in entries
        if not entry.snapshot["compilation"]
        and not entry.snapshot["partial"]
        and not entry.snapshot["canonical_id"]
    }
    items = []
    for entry, work in page:
        data = entry.snapshot
        released = date.fromisoformat(data["release_date"]) if data["release_date"] else None
        items.append(
            SeriesEntryView(
                membership_id=entry.id,
                external_id=data["book"]["external_id"],
                position=data["position"],
                details=data["details"],
                compilation=data["compilation"],
                partial=data["partial"],
                merged_record=bool(data["canonical_id"]),
                ambiguous_position=len(
                    by_position.get(
                        Decimal(data["position"]) if data["position"] is not None else None, set()
                    )
                )
                > 1
                or len(by_work.get(work.id, set())) > 1,
                release_date=data["release_date"],
                followed=work.id in followed_ids,
                publication="unreleased"
                if released and released > datetime.now(UTC).date()
                else "published"
                if released
                else "unknown",
                work=series_work_view(work, available[work.id], data),
            )
        )
    return SeriesView(
        id=row.id,
        external_id=external_id,
        name=row.name,
        description=row.snapshot.get("description"),
        generation=row.generation,
        fetched_at=row.fetched_at,
        status=status,
        message=message,
        items=items,
        total=len(entries),
        offset=offset,
        limit=limit,
        books=len(counted),
        owned=sum(available[key].owned for key in counted),
        ebook=sum(available[key].ebook for key in counted),
        audio=sum(available[key].audio for key in counted),
    )
