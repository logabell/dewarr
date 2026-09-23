"""Admin queue of library items that need a match or could not be fully read."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import Text, cast, func, literal, or_, select, union_all

from app.api.dependencies import Admin, Database
from app.api.library import AssetView, asset_views, open_url
from app.db.models import Integration, Library, LibraryAsset, LibraryReadIssue
from app.domain.library_review import open_assets, open_read_issues, reason_counts, review_counts

router = APIRouter(prefix="/library/review", tags=["library"])


class ReadIssueView(BaseModel):
    id: UUID
    library_id: UUID
    library_name: str
    server_kind: str
    external_id: str
    title: str
    authors: list[str]
    path: str | None
    reasons: list[str]
    last_seen_at: datetime
    open_url: str


class ReviewItem(BaseModel):
    kind: Literal["asset", "read-issue"]
    asset: AssetView | None = None
    read_issue: ReadIssueView | None = None


class ReviewPage(BaseModel):
    items: list[ReviewItem]
    total: int
    offset: int
    limit: int


class ReasonCount(BaseModel):
    reason: str
    count: int


class ReviewSummary(BaseModel):
    total: int
    needs_matching: int
    read_issues: int
    reasons: list[ReasonCount]


def _pattern(q: str) -> str:
    return "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("", response_model=ReviewPage)
async def review(
    admin: Admin,
    db: Database,
    kind: Literal["all", "needs-matching", "read-issue"] = "all",
    library_id: UUID | None = None,
    q: str = Query(default="", max_length=300),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=40, ge=1, le=100),
):
    assets = (
        open_assets(None if kind == "all" else kind)
        .join(Integration, Integration.id == Library.integration_id)
        .where(Integration.enabled.is_(True))
        .with_only_columns(
            literal("asset").label("source"),
            LibraryAsset.id.label("id"),
            func.coalesce(LibraryAsset.title, "").label("title"),
        )
    )
    issues = (
        open_read_issues()
        .join(Integration, Integration.id == Library.integration_id)
        .where(Integration.enabled.is_(True))
        .with_only_columns(
            literal("read-issue").label("source"),
            LibraryReadIssue.id.label("id"),
            func.coalesce(LibraryReadIssue.title, "").label("title"),
        )
    )
    if library_id:
        assets = assets.where(LibraryAsset.library_id == library_id)
        issues = issues.where(LibraryReadIssue.library_id == library_id)
    if q.strip():
        pattern = _pattern(q)
        assets = assets.where(
            or_(
                LibraryAsset.title.ilike(pattern),
                cast(LibraryAsset.metadata_snapshot["authors"], Text).ilike(pattern),
            )
        )
        issues = issues.where(
            or_(
                LibraryReadIssue.title.ilike(pattern),
                cast(LibraryReadIssue.authors, Text).ilike(pattern),
            )
        )
    combined = (union_all(assets, issues) if kind != "needs-matching" else assets).subquery()
    total = await db.scalar(select(func.count()).select_from(combined))
    page = (
        await db.execute(
            select(combined.c.source, combined.c.id)
            .order_by(func.lower(combined.c.title), combined.c.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    asset_ids = [row.id for row in page if row.source == "asset"]
    issue_ids = [row.id for row in page if row.source == "read-issue"]
    asset_rows = (
        await db.execute(
            select(LibraryAsset, Library, Integration)
            .join(Library, LibraryAsset.library_id == Library.id)
            .join(Integration, Library.integration_id == Integration.id)
            .where(LibraryAsset.id.in_(asset_ids))
        )
    ).all()
    by_asset = {view.id: view for view in await asset_views(db, admin, asset_rows)}
    issue_rows = (
        await db.execute(
            select(LibraryReadIssue, Library, Integration)
            .join(Library, LibraryReadIssue.library_id == Library.id)
            .join(Integration, Library.integration_id == Integration.id)
            .where(LibraryReadIssue.id.in_(issue_ids))
        )
    ).all()
    by_issue = {
        issue.id: ReadIssueView(
            id=issue.id,
            library_id=library.id,
            library_name=library.name,
            server_kind=connection.kind,
            external_id=issue.external_id,
            title=issue.title or "Unread item",
            authors=issue.authors or [],
            path=issue.path,
            reasons=issue.reasons or [],
            last_seen_at=issue.last_seen_at,
            open_url=open_url(connection, issue.external_id),
        )
        for issue, library, connection in issue_rows
    }
    items = [
        ReviewItem(kind="asset", asset=by_asset[row.id])
        if row.source == "asset"
        else ReviewItem(kind="read-issue", read_issue=by_issue[row.id])
        for row in page
    ]
    return ReviewPage(items=items, total=total or 0, offset=offset, limit=limit)


@router.get("/summary", response_model=ReviewSummary)
async def summary(admin: Admin, db: Database):
    enabled = (
        select(Library.id)
        .join(Integration, Integration.id == Library.integration_id)
        .where(Integration.enabled.is_(True))
    )
    counts = await review_counts(db, enabled)
    reasons = await reason_counts(db, enabled)
    return ReviewSummary(
        **counts,
        reasons=[ReasonCount(reason=reason, count=count) for reason, count in reasons],
    )
