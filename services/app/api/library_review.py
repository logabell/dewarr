"""Admin queue of library items that need a match or could not be fully read."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Text, cast, func, literal, or_, select, tuple_, union_all

from app.api.dependencies import Admin, Database
from app.api.library import AssetView, asset_views, open_url
from app.db.models import AssetContains, Integration, Library, LibraryAsset, LibraryReadIssue, Work
from app.domain.catalog_titles import identity_authors, parse_title_labels
from app.domain.library_review import (
    CLOSED_STATES,
    open_assets,
    open_read_issues,
    reason_counts,
    review_counts,
)
from app.importing.combine import statuses as combine_statuses

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


class PartSet(BaseModel):
    total: int
    present: list[int]
    library_id: UUID | None = None
    version_id: UUID | None = None
    # Whether Dewarr combines these parts into one book, and why not when it can't.
    combine_state: str | None = None
    combine_reason: str | None = None
    can_combine: bool = False


class AutoMatchCandidate(BaseModel):
    external_id: str
    title: str
    authors: list[str] = Field(default_factory=list)


class AutoMatch(BaseModel):
    """The background Hardcover matcher's last result for the linked book."""

    status: str
    reason: str | None = None
    checked_at: datetime | None = None
    candidates: list[AutoMatchCandidate] = Field(default_factory=list)


class ReviewItem(BaseModel):
    kind: Literal["asset", "read-issue"]
    asset: AssetView | None = None
    read_issue: ReadIssueView | None = None
    # Books this item is already linked to, so a matched item isn't mistaken for a missing one.
    linked_titles: list[str] = Field(default_factory=list)
    # What the library sent for each unreadable field, when it was a short scalar value.
    issue_values: dict[str, str] = Field(default_factory=dict)
    # Base title and identifying author, without recording labels or cast credits.
    search_query: str | None = None
    # The parts of this item's book that are in the same library.
    parts: PartSet | None = None
    auto_match: AutoMatch | None = None


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
    details: int = 0
    reasons: list[ReasonCount]


def _pattern(q: str) -> str:
    return "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("", response_model=ReviewPage)
async def review(
    admin: Admin,
    db: Database,
    kind: Literal["all", "needs-matching", "read-issue", "details"] = "all",
    library_id: UUID | None = None,
    q: str = Query(default="", max_length=300),
    reason: str | None = Query(default=None, max_length=40),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=40, ge=1, le=100),
):
    # A reason is one of the summary's problem counts, so it spans every tab.
    assets = (
        open_assets(None if kind == "all" else kind, reason)
        .join(Integration, Integration.id == Library.integration_id)
        .where(Integration.enabled.is_(True))
        .with_only_columns(
            literal("asset").label("source"),
            LibraryAsset.id.label("id"),
            func.coalesce(LibraryAsset.title, "").label("title"),
        )
    )
    issues = (
        open_read_issues(reason)
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
    only_assets = kind in {"needs-matching", "details"} and not reason
    combined = (assets if only_assets else union_all(assets, issues)).subquery()
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
    context = await asset_context(db, [asset for asset, _, _ in asset_rows], by_asset)
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
        ReviewItem(kind="asset", asset=by_asset[row.id], **context[row.id])
        if row.source == "asset"
        else ReviewItem(kind="read-issue", read_issue=by_issue[row.id])
        for row in page
    ]
    return ReviewPage(items=items, total=total or 0, offset=offset, limit=limit)


async def asset_context(db, assets, views):
    """Linked books, unreadable values, search text and part sets for each asset."""
    work_ids = {work_id for view in views.values() for work_id in view.work_ids}
    works = {
        work_id: (title, (fields or {}).get("auto_match"))
        for work_id, title, fields in (
            await db.execute(
                select(Work.id, Work.title, Work.metadata_fields).where(Work.id.in_(work_ids))
            )
            if work_ids
            else []
        )
    }
    parted = [
        (asset.library_id, views[asset.id].version_id)
        for asset in assets
        if views[asset.id].part_total and views[asset.id].version_id
    ]
    present = {}
    if parted:
        rows = await db.execute(
            select(
                LibraryAsset.library_id,
                LibraryAsset.version_id,
                AssetContains.part_index,
            )
            .join(AssetContains, AssetContains.asset_id == LibraryAsset.id)
            .where(
                tuple_(LibraryAsset.library_id, LibraryAsset.version_id).in_(parted),
                AssetContains.part_index.is_not(None),
                LibraryAsset.state.not_in(CLOSED_STATES),
            )
        )
        for library_id, version_id, index in rows:
            present.setdefault((library_id, version_id), set()).add(index)
    combining = (
        await combine_statuses(db, list({version for _, version in parted})) if parted else {}
    )
    context = {}
    for asset in assets:
        view = views[asset.id]
        snapshot = asset.metadata_snapshot or {}
        values = snapshot.get("read_issue_values")
        kept = identity_authors(view.authors)[0]
        query = " ".join(
            part for part in (parse_title_labels(view.title).title, kept[0] if kept else "") if part
        )
        parts = None
        if view.part_total and view.version_id:
            key = (asset.library_id, view.version_id)
            indexes = present.get(key, {view.part_index})
            status = combining.get(key) or {}
            parts = PartSet(
                total=view.part_total,
                present=sorted(indexes),
                library_id=asset.library_id,
                version_id=view.version_id,
                combine_state=status.get("state"),
                combine_reason=status.get("reason"),
                can_combine=bool(status.get("can_combine")),
            )
        auto_match = next(
            (
                works[work_id][1]
                for work_id in view.work_ids
                if work_id in works and isinstance(works[work_id][1], dict)
            ),
            None,
        )
        try:
            auto_match = AutoMatch.model_validate(auto_match) if auto_match else None
        except ValidationError:
            auto_match = None
        context[asset.id] = {
            "auto_match": auto_match,
            "linked_titles": [works[work_id][0] for work_id in view.work_ids if work_id in works],
            "issue_values": {
                key: value
                for key, value in (values if isinstance(values, dict) else {}).items()
                if key in (asset.read_issues or []) and isinstance(value, str)
            },
            "search_query": query[:300] or None,
            "parts": parts,
        }
    return context


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
