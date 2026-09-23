"""Library items for an admin to review: unmatched books and records Dewarr could not read."""

from sqlalchemy import func, or_, select, union_all

from app.db.models import Library, LibraryAsset, LibraryReadIssue

# Gone from the library, so there is nothing left to link.
CLOSED_STATES = ("missing-confirmed", "intentionally-removed")
NEEDS_MATCHING = LibraryAsset.match_status == "needs-review"
HAS_READ_ISSUES = func.jsonb_array_length(LibraryAsset.read_issues) > 0


def open_assets(kind: str | None = None):
    condition = {
        "needs-matching": NEEDS_MATCHING,
        "read-issue": HAS_READ_ISSUES,
    }.get(kind, or_(NEEDS_MATCHING, HAS_READ_ISSUES))
    return (
        select(LibraryAsset)
        .join(Library, Library.id == LibraryAsset.library_id)
        .where(Library.accessible, LibraryAsset.state.not_in(CLOSED_STATES), condition)
    )


def open_read_issues():
    return (
        select(LibraryReadIssue)
        .join(Library, Library.id == LibraryReadIssue.library_id)
        .where(Library.accessible, LibraryReadIssue.resolved_at.is_(None))
    )


async def review_counts(db, library_ids=None) -> dict[str, int]:
    assets, issues = open_assets(), open_read_issues()
    if library_ids is not None:
        assets = assets.where(LibraryAsset.library_id.in_(library_ids))
        issues = issues.where(LibraryReadIssue.library_id.in_(library_ids))
    matching = await db.scalar(
        select(func.count()).select_from(assets.where(NEEDS_MATCHING).subquery())
    )
    unread_assets = await db.scalar(
        select(func.count()).select_from(assets.where(HAS_READ_ISSUES).subquery())
    )
    unread_items = await db.scalar(select(func.count()).select_from(issues.subquery()))
    total = await db.scalar(select(func.count()).select_from(assets.subquery()))
    return {
        "total": total + unread_items,
        "needs_matching": matching,
        "read_issues": unread_assets + unread_items,
    }


async def reason_counts(db, library_ids) -> list[tuple[str, int]]:
    asset_reasons = (
        open_assets("read-issue")
        .where(LibraryAsset.library_id.in_(library_ids))
        .with_only_columns(func.jsonb_array_elements_text(LibraryAsset.read_issues).label("reason"))
    )
    item_reasons = (
        open_read_issues()
        .where(LibraryReadIssue.library_id.in_(library_ids))
        .with_only_columns(func.jsonb_array_elements_text(LibraryReadIssue.reasons).label("reason"))
    )
    reasons = union_all(asset_reasons, item_reasons).subquery()
    rows = await db.execute(
        select(reasons.c.reason, func.count().label("count"))
        .group_by(reasons.c.reason)
        .order_by(func.count().desc(), reasons.c.reason)
        .limit(20)
    )
    return [(row.reason, row.count) for row in rows]


def review_message(counts: dict[str, int]) -> str:
    total = counts["total"]
    if not total:
        return ""
    return f". {total} {'item needs' if total == 1 else 'items need'} review"
