"""Follow a book until its release day, then retry the wanted request on the existing schedule."""

from datetime import UTC, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.db.models import (
    AssetContains,
    DiscoveryLayout,
    Integration,
    Library,
    LibraryAsset,
    MonitoredRelease,
    Operation,
    User,
    Work,
)
from app.db.session import session_factory
from app.domain.availability import availability_for
from app.domain.operations import transaction_lock
from app.domain.release_dates import (
    GENRES,
    assign_release,
    due_action,
    release_facts,
    replaces_release,
    retry_at,
)
from app.domain.visibility import visible_library, visible_work
from app.domain.work_graph import canonical_work


async def saved_genres(db, user_id):
    row = await db.get(DiscoveryLayout, user_id)
    raw = (row.preferences or {}).get("release_genres", []) if row else []
    return [genre for genre in raw if genre in GENRES]


async def save_genres(db, user, genres):
    await transaction_lock(db, f"discovery-layout:{user.id}")
    row = await db.get(DiscoveryLayout, user.id)
    if not row:
        row = DiscoveryLayout(user_id=user.id, preferences={})
        db.add(row)
    selected = [genre for genre in genres if genre in GENRES]
    row.preferences = {**(row.preferences or {}), "release_genres": selected}
    return selected


async def _monitor(db, user, work):
    row = await db.scalar(
        select(MonitoredRelease).where(
            MonitoredRelease.owner_id == user.id,
            MonitoredRelease.work_id == work.id,
        )
    )
    if not row:
        row = MonitoredRelease(owner_id=user.id, work_id=work.id)
        db.add(row)
        await db.flush()
    return row


async def sync_monitor(db, user, work, operation, specification):
    """Record a monitored request when the book has a release day or is explicitly unreleased."""
    day, basis, coming = release_facts(work.metadata_fields)
    if day is None and not coming:
        return None
    row = await _monitor(db, user, work)
    if row.state == "stopped":
        return row
    row.release_date = day
    row.basis = basis if day else "unknown"
    row.specification = specification or {}
    row.operation_id = operation.id
    now = datetime.now(UTC)
    if "waiting_for_release" in operation.payload:
        row.state = "waiting"
        row.next_check_at = datetime.combine(day, time.min, tzinfo=UTC) if day else None
        return row
    if operation.status == "completed" and "Already available" in (operation.message or ""):
        row.state = "available"
        row.next_check_at = None
        return row
    row.state = "wanted"
    row.round = max(row.round, 0) + 1
    row.next_check_at = retry_at(row.round, now)
    return row


async def stop(db, user, work_id):
    work = await canonical_work(db, work_id)
    row = await db.scalar(
        select(MonitoredRelease).where(
            MonitoredRelease.owner_id == user.id,
            MonitoredRelease.work_id == work.id,
            MonitoredRelease.state != "stopped",
        )
    )
    if not row:
        return None
    row.state = "stopped"
    row.next_check_at = None
    row.generation += 1
    return row


def _owned(info, specification):
    if not info:
        return False
    mode = (specification or {}).get("mode")
    if mode == "audio":
        return info.audio
    if mode == "ebook":
        return info.ebook
    return info.owned


async def record_release_day(db, row, work, day, basis, *, source):
    """Store the first release day, or replace a work day with an audiobook day."""
    if row.state != "waiting" or not replaces_release(row.release_date, row.basis, day, basis):
        return False
    work.metadata_fields = assign_release(
        work.metadata_fields,
        day,
        basis if basis in {"audiobook", "work", "unknown"} else "unknown",
        coming_soon=False,
        source=source,
    )
    stored, stored_basis, _ = release_facts(work.metadata_fields)
    if stored is None or (row.release_date == stored and row.basis == stored_basis):
        return False
    row.release_date = stored
    row.basis = stored_basis
    row.next_check_at = datetime.combine(stored, time.min, tzinfo=UTC)
    if row.operation_id:
        operation = await db.get(Operation, row.operation_id)
        raw = operation.payload if operation else None
        if isinstance(raw, dict) and "waiting_for_release" in raw:
            payload = dict(raw)
            payload["waiting_for_release"] = stored.isoformat()
            operation.payload = payload
            operation.message = (
                f"Waiting until {stored.isoformat()}; source search starts on release day"
            )
    return True


async def adopt_release_days(db, user, discover):
    """The month feed can correct a followed book that is still waiting on a weaker day."""
    books = {}
    for item in discover:
        if item.get("work_id") and item.get("release_date"):
            books[item["work_id"]] = item
    if not books:
        return False
    rows = list(
        await db.scalars(
            select(MonitoredRelease).where(
                MonitoredRelease.owner_id == user.id,
                MonitoredRelease.work_id.in_(list(books)),
                MonitoredRelease.state == "waiting",
            )
        )
    )
    changed = False
    for row in rows:
        item = books[row.work_id]
        work = await db.get(Work, row.work_id)
        basis = item.get("basis") if item.get("basis") in {"audiobook", "work"} else "unknown"
        if work and await record_release_day(
            db, row, work, item["release_date"], basis, source="hardcover"
        ):
            changed = True
    return changed


async def advance(db, row, today, now):
    from app.domain.acquisition import RequestOptions
    from app.domain.quick_add import begin, resume_search

    user = await db.get(User, row.owner_id)
    if not user or not user.active or user.role == "viewer":
        row.next_check_at = now + timedelta(days=1)
        return
    work = await canonical_work(db, row.work_id)
    info = (await availability_for(db, user, [work.id])).get(work.id)
    operation = await db.get(Operation, row.operation_id) if row.operation_id else None
    if operation and operation.kind == "lists.release-wait":
        from app.domain.follows import release_ready

        await release_ready(db, row, operation, now)
        return
    in_flight = bool(operation and operation.status in {"queued", "running"})
    failed = bool(
        operation and operation.status == "held" and "waiting_for_release" not in operation.payload
    )
    action = due_action(
        state=row.state,
        release_date=row.release_date,
        today=today,
        now=now,
        next_check_at=row.next_check_at,
        owned=_owned(info, row.specification),
        in_flight=in_flight,
        failed=failed,
    )
    if action == "available":
        row.state = "available"
        row.next_check_at = None
        return
    if action in {"idle", "hold"}:
        return
    if action == "wait":
        # A running search is checked shortly. A finished grab is polled for ownership,
        # without starting another download.
        row.next_check_at = now + timedelta(minutes=10) if in_flight else now + timedelta(hours=6)
        return
    if action == "resume" and operation and "waiting_for_release" in (operation.payload or {}):
        await resume_search(db, user, operation)
        if row.state == "waiting" and operation.status not in {"queued", "running"}:
            row.next_check_at = now + timedelta(days=1)
        return
    options = RequestOptions.model_validate(row.specification or {})
    stamp = row.next_check_at
    await begin(
        db,
        user,
        work.id,
        options,
        f"release-check:{row.id}:{row.generation}:{row.round + 1}",
    )
    # An existing operation can return without scheduling the next check.
    if (
        row.state in {"waiting", "wanted"}
        and row.next_check_at == stamp
        and (stamp is None or stamp <= now)
    ):
        row.next_check_at = now + timedelta(hours=6)


async def schedule():
    from app.config import get_settings

    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        now = datetime.now(UTC)
        today = now.date()
        rows = list(
            await db.scalars(
                select(MonitoredRelease)
                .where(
                    MonitoredRelease.state.in_(["waiting", "wanted"]),
                    MonitoredRelease.release_date.is_not(None),
                    MonitoredRelease.release_date <= today,
                    or_(
                        MonitoredRelease.next_check_at.is_(None),
                        MonitoredRelease.next_check_at <= now,
                    ),
                )
                .order_by(MonitoredRelease.next_check_at, MonitoredRelease.id)
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            try:
                async with db.begin_nested():
                    await advance(db, row, today, now)
            except HTTPException:
                await db.refresh(row)
                row.next_check_at = now + timedelta(days=1)


async def personal_entries(db, user, start, end):
    """Followed, quick-added, and library books for this month, plus undated follows."""
    monitors = list(
        await db.scalars(
            select(MonitoredRelease).where(
                MonitoredRelease.owner_id == user.id,
                MonitoredRelease.state != "stopped",
                or_(
                    MonitoredRelease.release_date.is_(None),
                    MonitoredRelease.release_date.between(start, end),
                ),
            )
        )
    )
    works = {}
    if monitors:
        found = await db.scalars(select(Work).where(Work.id.in_([row.work_id for row in monitors])))
        works = {work.id: work for work in found}
    library_rows = (
        (
            await db.execute(
                select(AssetContains.work_id, LibraryAsset.medium)
                .join(LibraryAsset, LibraryAsset.id == AssetContains.asset_id)
                .join(Library, Library.id == LibraryAsset.library_id)
                .join(Integration, Integration.id == Library.integration_id)
                .where(
                    AssetContains.work_id.in_(list(works)),
                    AssetContains.verified.is_(True),
                    LibraryAsset.state.in_(["present", "stale"]),
                    Library.accessible.is_(True),
                    Integration.enabled.is_(True),
                    visible_library(user),
                )
            )
        ).all()
        if works
        else []
    )
    library_ids = {work_id for work_id, _medium in library_rows}
    audio_ids = {work_id for work_id, medium in library_rows if medium == "audio"}
    entries = []
    seen = set()
    for row in monitors:
        work = works.get(row.work_id)
        if not work:
            continue
        day, basis, _ = release_facts(work.metadata_fields)
        shown = row.release_date or day
        if row.release_date is None and day is not None and not start <= day <= end:
            continue
        seen.add(work.id)
        entries.append(
            {
                "work_id": work.id,
                "external_id": None,
                "provider": "local",
                "title": work.title,
                "authors": work.authors,
                "cover_url": work.cover_url,
                "release_date": shown,
                "basis": row.basis or basis,
                "followed": True,
                "in_library": work.id
                in (audio_ids if (row.basis or basis) == "audiobook" else library_ids),
                "genres": [],
                "state": row.state,
            }
        )
    library_filter = [
        visible_work(user),
        AssetContains.verified.is_(True),
        LibraryAsset.state.in_(["present", "stale"]),
        Library.accessible.is_(True),
        Integration.enabled.is_(True),
        visible_library(user),
        Work.metadata_fields["release"]["date"].astext >= start.isoformat(),
        Work.metadata_fields["release"]["date"].astext <= end.isoformat(),
    ]
    if seen:
        library_filter.append(Work.id.not_in(seen))
    held = list(
        await db.execute(
            select(Work, LibraryAsset.medium)
            .join(AssetContains, AssetContains.work_id == Work.id)
            .join(LibraryAsset, LibraryAsset.id == AssetContains.asset_id)
            .join(Library, Library.id == LibraryAsset.library_id)
            .join(Integration, Integration.id == Library.integration_id)
            .where(*library_filter)
        )
    )
    grouped = {}
    for work, medium in held:
        bucket = grouped.setdefault(work.id, {"work": work, "audio": False})
        if medium == "audio":
            bucket["audio"] = True
    for bucket in grouped.values():
        work = bucket["work"]
        if work.id in seen:
            continue
        seen.add(work.id)
        day, basis, _ = release_facts(work.metadata_fields)
        if day and not start <= day <= end:
            continue
        entries.append(
            {
                "work_id": work.id,
                "external_id": None,
                "provider": "local",
                "title": work.title,
                "authors": work.authors,
                "cover_url": work.cover_url,
                "release_date": day,
                "basis": basis,
                "followed": False,
                "in_library": bucket["audio"] if basis == "audiobook" else True,
                "genres": [],
                "state": None,
            }
        )
    return entries
