"""Library items for an admin to review: unmatched books and records Dewarr could not read."""

from sqlalchemy import and_, func, not_, or_, select, union_all
from sqlalchemy.dialects.postgresql import array

from app.adapters.audiobookshelf import IDENTITY_ISSUES
from app.db.models import Library, LibraryAsset, LibraryReadIssue

# Gone from the library, so there is nothing left to link.
CLOSED_STATES = ("missing-confirmed", "intentionally-removed")
NEEDS_MATCHING = LibraryAsset.match_status == "needs-review"
HAS_READ_ISSUES = func.jsonb_array_length(LibraryAsset.read_issues) > 0
# A lost title or author can hold matching. Other unreadable fields are only missing details.
HAS_IDENTITY_ISSUES = LibraryAsset.read_issues.has_any(array(sorted(IDENTITY_ISSUES)))
DETAILS_ONLY = and_(HAS_READ_ISSUES, not_(HAS_IDENTITY_ISSUES))


def open_assets(kind: str | None = None, reason: str | None = None):
    condition = {
        "needs-matching": NEEDS_MATCHING,
        "read-issue": HAS_IDENTITY_ISSUES,
        "details": DETAILS_ONLY,
        "any": or_(NEEDS_MATCHING, HAS_READ_ISSUES),
    }.get(kind, or_(NEEDS_MATCHING, HAS_IDENTITY_ISSUES))
    if reason:
        condition = and_(
            or_(NEEDS_MATCHING, HAS_READ_ISSUES), LibraryAsset.read_issues.contains([reason])
        )
    return (
        select(LibraryAsset)
        .join(Library, Library.id == LibraryAsset.library_id)
        .where(Library.accessible, LibraryAsset.state.not_in(CLOSED_STATES), condition)
    )


def open_read_issues(reason: str | None = None):
    query = (
        select(LibraryReadIssue)
        .join(Library, Library.id == LibraryReadIssue.library_id)
        .where(Library.accessible, LibraryReadIssue.resolved_at.is_(None))
    )
    return query.where(LibraryReadIssue.reasons.contains([reason])) if reason else query


async def review_counts(db, library_ids=None) -> dict[str, int]:
    assets, issues = open_assets("any"), open_read_issues()
    if library_ids is not None:
        assets = assets.where(LibraryAsset.library_id.in_(library_ids))
        issues = issues.where(LibraryReadIssue.library_id.in_(library_ids))

    async def count(query):
        return await db.scalar(select(func.count()).select_from(query.subquery()))

    matching = await count(assets.where(NEEDS_MATCHING))
    unread_assets = await count(assets.where(HAS_IDENTITY_ISSUES))
    details = await count(assets.where(DETAILS_ONLY))
    unread_items = await count(issues)
    attention = await count(assets.where(or_(NEEDS_MATCHING, HAS_IDENTITY_ISSUES)))
    return {
        "total": attention + unread_items,
        "needs_matching": matching,
        "read_issues": unread_assets + unread_items,
        "details": details,
    }


async def reason_counts(db, library_ids) -> list[tuple[str, int]]:
    asset_reasons = (
        open_assets("any")
        .where(HAS_READ_ISSUES, LibraryAsset.library_id.in_(library_ids))
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
