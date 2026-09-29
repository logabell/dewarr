"""Read-only catalog artwork and current automatic acquisition progress for requests."""

from uuid import UUID

from sqlalchemy import select

from app.db.models import CatalogSeries, Operation, SeriesMembership
from app.domain.identity import normalized
from app.domain.title_matching import compatible_title
from app.domain.work_graph import family_ids


async def series_cover(db, owner_id, work):
    """Reuse the requester's observed catalog art without changing work identity."""
    snapshots = await db.scalars(
        select(SeriesMembership.snapshot)
        .join(CatalogSeries, CatalogSeries.id == SeriesMembership.series_id)
        .where(
            CatalogSeries.owner_id == owner_id,
            SeriesMembership.work_id.in_(family_ids(work.id)),
            SeriesMembership.present.is_(True),
        )
        .order_by(CatalogSeries.fetched_at.desc().nulls_last(), SeriesMembership.id)
    )
    for snapshot in snapshots:
        book = snapshot.get("book") or {}
        if (
            book.get("cover_url")
            and compatible_title(book.get("title", ""), work.title)
            and sorted(normalized(a) for a in book.get("authors", []))
            == sorted(normalized(a) for a in work.authors or [])
        ):
            return book["cover_url"]
    return work.cover_url


async def series_progress(db, intent, reasons, target):
    """Do not leave an old source review on screen while its controller retries."""
    if target.state != "wanted" or target.attempt_id:
        return
    parents = []
    for reason in reasons:
        if reason.kind == "series" and reason.active and reason.approval_status == "approved":
            try:
                parents.append(UUID(reason.reference))
            except (ValueError, TypeError):
                continue
    if not parents:
        return
    controllers = await db.scalars(
        select(Operation)
        .where(
            Operation.owner_id == intent.owner_id,
            Operation.kind == "series.acquire",
            Operation.payload["parent_id"].astext.in_([str(p) for p in parents]),
            Operation.status.in_(["queued", "running"]),
        )
        .order_by(Operation.created_at.desc(), Operation.id.desc())
    )
    for controller in controllers:
        if not controller.payload.get("enabled"):
            continue
        book = next(
            (
                b
                for b in controller.payload.get("books", {}).values()
                if b.get("request_id") == str(intent.id)
            ),
            None,
        )
        if not book:
            continue
        progress = book.get("progress", {}).get(target.slot, {})
        # Once a current selection exists its own result is authoritative.
        if progress.get("selection_id"):
            continue
        if progress.get("search_id"):
            target.selection_status = "searching"
            target.message = "Searching sources for this format"
        elif progress.get("next_at"):
            target.selection_status = "waiting"
            target.message = "No eligible source yet. Automatic search will retry."
        elif controller.status == "queued" or book.get("state") == "wanted":
            target.selection_status = "scheduled"
            target.message = "Request started. Source search is queued."
        else:
            continue
        target.next_action = "none"
        return
