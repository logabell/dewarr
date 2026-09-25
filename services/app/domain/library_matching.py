"""Match library books to Hardcover in the background after a sync.

Only a unique, verified match is saved, with the same evidence fences as the
reader's "Save match" action. Anything else keeps its candidates for review.
"""

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, func, select, tuple_

from app.adapters.contracts import AdapterError, FailureKind
from app.config import get_settings
from app.db.models import (
    AssetContains,
    AuditEvent,
    CatalogAccount,
    Integration,
    Library,
    LibraryAsset,
    Operation,
    User,
    Work,
    WorkMetadataSource,
)
from app.db.session import session_factory
from app.domain.catalog_metadata import attach_source, preferences
from app.domain.work_graph import family_ids
from app.jobs.queue import enqueue

TERMINAL = {"completed", "cancelled", "failed"}
# Each book costs one to four Hardcover requests spaced about a second apart.
BATCH = 25


async def schedule_library_match(db, owner_id, integration_id, run_id):
    """Queue one matching pass for a finished sync, unless the admin turned matching off."""
    if get_settings().recovery_mode or not (await preferences(db)).automatic_library_matching:
        return None
    account = await db.get(CatalogAccount, owner_id)
    if not account or not account.enabled:
        return None
    running = await db.scalar(
        select(Operation.id).where(
            Operation.kind == "library.match",
            Operation.integration_id == integration_id,
            Operation.status.not_in(TERMINAL),
        )
    )
    if running:
        return None
    operation = Operation(
        owner_id=owner_id,
        kind="library.match",
        idempotency_key=f"library-match:{run_id}",
        integration_id=integration_id,
        payload={"run_id": str(run_id), "account_generation": account.generation},
        message="Waiting to match library books to Hardcover",
    )
    db.add(operation)
    await db.flush()
    operation.job_id = await enqueue(
        db,
        "library.match",
        operation_id=str(operation.id),
        job_lock=f"library-match:{operation.id}",
    )
    return operation


def evidence_hash(identity):
    return hashlib.sha256(identity[1].encode()).hexdigest()


async def candidates(db, integration_id, cursor=None):
    """Provisional library books with no Hardcover decision, oldest first."""
    decided = exists(
        select(WorkMetadataSource.id).where(
            WorkMetadataSource.work_id == Work.id, WorkMetadataSource.provider == "hardcover"
        )
    )
    held = (
        select(AssetContains.work_id)
        .join(LibraryAsset, LibraryAsset.id == AssetContains.asset_id)
        .join(Library, Library.id == LibraryAsset.library_id)
        .where(
            Library.integration_id == integration_id,
            Library.accessible.is_(True),
            AssetContains.verified.is_(True),
            LibraryAsset.state.in_(["present", "stale"]),
        )
    )
    return list(
        (
            await db.execute(
                select(Work.id, Work.metadata_fields, Work.created_at)
                .where(
                    Work.id.in_(held),
                    Work.provisional.is_(True),
                    Work.redirect_to.is_(None),
                    ~decided,
                    tuple_(Work.created_at, Work.id)
                    > (datetime.fromisoformat(cursor[0]), UUID(cursor[1]))
                    if cursor
                    else True,
                )
                .order_by(Work.created_at, Work.id)
                .limit(BATCH + 1)
            )
        ).all()
    )


async def match_library(operation_id):
    from app.api.metadata import provider_call, reader_lookup_identity
    from app.domain.hardcover_matching import MatchEvidence
    from app.domain.hardcover_matching import lookup as lookup_hardcover

    db = session_factory()()
    try:
        operation = await db.get(Operation, operation_id, with_for_update=True)
        if not operation or operation.kind != "library.match" or operation.status in TERMINAL:
            return
        owner_id, integration_id = operation.owner_id, operation.integration_id
        user = await db.get(User, owner_id)
        account = await db.get(CatalogAccount, owner_id)
        settings = await preferences(db)
        if (
            get_settings().recovery_mode
            or not settings.automatic_library_matching
            or not user
            or not user.active
            or user.role != "admin"
            or not account
            or not account.enabled
            or account.generation != operation.payload.get("account_generation")
        ):
            operation.status, operation.message = (
                "cancelled",
                "Automatic matching was turned off or the Hardcover connection changed",
            )
            await db.commit()
            return
        operation.status, operation.message = "running", "Matching library books to Hardcover"
        cursor = operation.payload.get("cursor")
        previous = dict(operation.payload)
        rows = await candidates(db, integration_id, cursor)
        await db.commit()

        matched = checked = 0
        paused = False
        series = {}
        retry_after = 0
        for work_id, fields, created_at in rows[:BATCH]:
            next_cursor = [created_at.isoformat(), str(work_id)]
            user = await db.get(User, owner_id, populate_existing=True)
            identity = await reader_lookup_identity(db, user, work_id)
            if not identity:
                cursor = next_cursor
                continue
            fingerprint = evidence_hash(identity)
            if (fields or {}).get("auto_match", {}).get("evidence") == fingerprint:
                # Already tried with exactly this evidence. A resync with new details retries.
                cursor = next_cursor
                continue
            checked += 1

            async def call(operation_name, *args):
                return await provider_call(db, owner_id, "hardcover", operation_name, *args)

            try:
                result = await lookup_hardcover(
                    MatchEvidence.model_validate_json(identity[1]), call
                )
            except AdapterError as error:
                await db.rollback()
                if error.kind in {FailureKind.RATE_LIMIT, FailureKind.UNAVAILABLE}:
                    paused = True
                    retry_after = error.retry_after or 60
                    checked -= 1
                    break
                result = None
            except HTTPException:
                # The Hardcover connection changed during the pass.
                await db.rollback()
                paused = True
                retry_after = 60
                checked -= 1
                break
            saved = await record(db, owner_id, work_id, identity, fingerprint, result)
            matched += saved
            cursor = next_cursor
            if saved and settings.write_library_series:
                entry = next(
                    (item for item in result.book.series if item.name and not item.compilation),
                    None,
                )
                if entry:
                    position = entry.position
                    if position and position.endswith(".0"):
                        position = position[:-2]
                    series[work_id] = (entry.name.strip()[:600], position)

        written, warning = await write_series(db, owner_id, integration_id, series)
        operation = await db.get(Operation, operation_id, with_for_update=True)
        more = paused or len(rows) > BATCH
        operation.status = "queued" if more else "completed"
        matched += previous.get("matched", 0)
        checked += previous.get("checked", 0)
        written += previous.get("series_written", 0)
        operation.message = (
            f"Matched {matched} of {checked} library books to Hardcover"
            + (f"; added the series to {written} Audiobookshelf items" if written else "")
            + ("; Hardcover asked us to wait; matching will resume automatically" if paused else "")
            + (f"; {warning}" if warning else "")
        )
        operation.payload = {
            **operation.payload,
            "cursor": cursor,
            "matched": matched,
            "checked": checked,
            "series_written": written,
        }
        if more:
            operation.job_id = await enqueue(
                db,
                "library.match",
                operation_id=str(operation.id),
                job_lock=f"library-match:{operation.id}",
                schedule_in={"seconds": max(1, retry_after)},
            )
        await db.commit()
    finally:
        await db.close()


async def write_series(db, owner_id, integration_id, series):
    """Set the matched series on this connection's Audiobookshelf items that have none.

    Returns the number of items written and a warning when writing stopped early.
    """
    from app.adapters.audiobookshelf import Audiobookshelf
    from app.domain.libro_library import library_token

    if not series:
        return 0, None
    integration = await db.get(Integration, integration_id)
    if not integration or not integration.enabled or integration.kind != "audiobookshelf":
        return 0, None
    rows = (
        await db.execute(
            select(LibraryAsset.id, LibraryAsset.external_id, AssetContains.work_id)
            .join(AssetContains, AssetContains.asset_id == LibraryAsset.id)
            .join(Library, Library.id == LibraryAsset.library_id)
            .where(
                Library.integration_id == integration_id,
                Library.accessible.is_(True),
                AssetContains.work_id.in_(list(series)),
                AssetContains.verified.is_(True),
                LibraryAsset.state.in_(["present", "stale"]),
                func.coalesce(func.jsonb_array_length(LibraryAsset.metadata_snapshot["series"]), 0)
                == 0,
            )
            .order_by(LibraryAsset.external_id)
        )
    ).all()
    base_url, secrets = integration.base_url, integration.encrypted_secrets
    await db.rollback()
    written, done, warning = 0, [], None
    try:
        async with Audiobookshelf(base_url, library_token(secrets)) as library:
            for asset_id, item_id, work_id in rows:
                name, sequence = series[work_id]
                await library.update_series(item_id, name, sequence)
                written += 1
                done.append((asset_id, item_id, work_id))
    except AdapterError as error:
        warning = f"series write-back stopped: {error}"
    for asset_id, item_id, work_id in done:
        name, sequence = series[work_id]
        db.add(
            AuditEvent(
                actor_id=owner_id,
                action="library.series.written",
                entity_id=asset_id,
                detail={"item_id": item_id, "series": name, "sequence": sequence},
            )
        )
    await db.commit()
    return written, warning


async def record(db, owner_id, work_id, identity, fingerprint, result):
    """Save a verified match, or remember the outcome. Returns 1 when a match was saved."""
    from app.api.metadata import reader_lookup_identity

    user = await db.get(User, owner_id, populate_existing=True)
    work = await db.get(Work, work_id, with_for_update=True, populate_existing=True)
    if not work or not user or await reader_lookup_identity(db, user, work_id) != identity:
        # The book changed while Hardcover was being asked. The next sync tries again.
        await db.rollback()
        return 0
    decided = await db.scalar(
        select(WorkMetadataSource.id).where(
            WorkMetadataSource.work_id.in_(family_ids(work.id)),
            WorkMetadataSource.provider == "hardcover",
        )
    )
    saved = 0
    outcome = {
        "evidence": fingerprint,
        "checked_at": datetime.now(UTC).isoformat(),
        "status": result.status if result else "unmatched",
        "reason": result.reason if result else "Hardcover could not be read for this book.",
        "candidates": [
            {"external_id": book.external_id, "title": book.title, "authors": book.authors}
            for book in (result.candidates if result else [])[:5]
        ],
    }
    if result and result.book and not decided:
        try:
            async with db.begin_nested():
                await attach_source(db, work, result.book, verified_match=True)
        except HTTPException as error:
            outcome["status"], outcome["reason"] = "unmatched", str(error.detail)
        else:
            saved = 1
            db.add(
                AuditEvent(
                    actor_id=owner_id,
                    action="metadata.source.auto-matched",
                    entity_id=work.id,
                    detail={
                        "provider": "hardcover",
                        "external_id": result.book.external_id,
                        "basis": result.basis,
                        "automatic": True,
                    },
                )
            )
    work.metadata_fields = {**work.metadata_fields, "auto_match": outcome}
    await db.commit()
    return saved
