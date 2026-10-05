from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text

from app.api.dependencies import Admin, Database
from app.api.imports import assert_admin
from app.config import get_settings
from app.db.models import (
    AuditEvent,
    ImportDestination,
    ImportEntry,
    ImportRun,
    Operation,
)
from app.importing.destinations import destination_configuration
from app.importing.starting import ImportInput
from app.importing.starting import start_import as start_import_command
from app.jobs.queue import enqueue

router = APIRouter(prefix="/organization", tags=["organization"])


class CoverExportView(BaseModel):
    state: Literal["prepared", "unavailable"]
    message: str
    sha256: str | None = None
    backend_selected: bool | None = None
    unchanged: bool | None = None


class EntryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    group_id: UUID
    version_id: UUID
    destination_id: UUID | None
    operation_id: UUID | None
    state: str
    message: str
    published_at: datetime | None
    confirmed_at: datetime | None
    asset_id: UUID | None
    can_retry: bool = False
    can_cancel: bool = False
    bookdrop_url: str | None = None
    cover_export: CoverExportView | None = None


class RunView(BaseModel):
    id: UUID
    plan_id: UUID
    created_at: datetime
    entries: list[EntryView]


async def view(db, run):
    return (await views(db, [run]))[0]


async def views(db, runs):
    """Read status without materializing publication manifests or frozen credentials."""
    from app.domain.recovery_approvals import denial

    if not runs:
        return []
    holds = {
        plan_id: await denial(db, "import-plan", plan_id)
        for plan_id in {run.plan_id for run in runs}
    }
    entries = (
        await db.execute(
            select(
                ImportEntry.id,
                ImportEntry.run_id,
                ImportEntry.group_id,
                ImportEntry.version_id,
                ImportEntry.destination_id,
                ImportEntry.operation_id,
                ImportEntry.state,
                ImportEntry.message,
                ImportEntry.published_at,
                ImportEntry.confirmed_at,
                ImportEntry.asset_id,
                ImportEntry.cover_export,
                ImportEntry.reserved,
                func.coalesce(
                    (func.jsonb_typeof(ImportEntry.specification) == "object")
                    & (ImportEntry.specification != {}),
                    False,
                ).label("has_specification"),
                ImportEntry.configuration["destination"]["workflow"].astext.label("workflow"),
                ImportEntry.configuration["destination"]["backend"]["base_url"].astext.label(
                    "base_url"
                ),
            )
            .where(ImportEntry.run_id.in_([run.id for run in runs]))
            .order_by(ImportEntry.created_at, ImportEntry.id)
        )
    ).all()
    by_run = {run.id: [] for run in runs}
    by_plan = {run.id: run.plan_id for run in runs}
    for entry in entries:
        hold = holds[by_plan[entry.run_id]]
        by_run[entry.run_id].append(
            EntryView.model_validate(entry).model_copy(
                update={
                    "bookdrop_url": entry.base_url.rstrip("/") + "/bookdrop"
                    if entry.workflow == "bookdrop" and entry.base_url
                    else None,
                    "can_retry": bool(
                        not hold
                        and entry.reserved
                        and entry.has_specification
                        and entry.state in {"held", "awaiting-library"}
                    ),
                    "can_cancel": not hold
                    and not entry.published_at
                    and entry.state in {"queued", "publishing", "held", "cancel-held"},
                    **({"message": hold} if hold and not entry.confirmed_at else {}),
                }
            )
        )
    return [
        RunView(id=run.id, plan_id=run.plan_id, created_at=run.created_at, entries=by_run[run.id])
        for run in runs
    ]


@router.post("/plans/{plan_id}/imports", response_model=RunView, status_code=202)
async def start_import(
    plan_id: UUID,
    body: ImportInput,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    run = await start_import_command(db, admin, plan_id, body, idempotency_key)
    await db.commit()
    return await view(db, run)


@router.post("/imports/{run_id}/entries/{entry_id}/cancel", response_model=RunView, status_code=202)
async def cancel_entry(run_id: UUID, entry_id: UUID, admin: Admin, db: Database):
    await assert_admin(db, admin.id)
    if get_settings().recovery_mode:
        raise HTTPException(409, "Import changes are paused for recovery")
    run = await db.scalar(
        select(ImportRun).where(ImportRun.id == run_id, ImportRun.owner_id == admin.id)
    )
    entry = await db.get(ImportEntry, entry_id, with_for_update=True)
    if not run or not entry or entry.run_id != run.id:
        raise HTTPException(404, "Import entry not found")
    if entry.state == "cancelled":
        return await view(db, run)
    from app.domain.recovery_approvals import require_current

    await require_current(db, "import-plan", run.plan_id)
    if entry.published_at or entry.state in {"confirmed", "skipped", "awaiting-library"}:
        raise HTTPException(
            409, "This book was already published or satisfied; its files are preserved"
        )
    if entry.state == "cancelling":
        return await view(db, run)  # Periodic recovery handles failed/expired queue attempts.
    entry.run_token, entry.next_check_at = None, None
    if not entry.specification and not entry.reserved:
        entry.state = "cancelled"
        entry.message = "Unstarted import stopped; you can review a new plan"
    else:
        operation = await db.get(Operation, entry.operation_id)
        status = await db.scalar(
            text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
            {"id": operation.job_id},
        )
        entry.state = "cancelling"
        entry.message = "Stopping import after checking whether any files were already published"
        entry.next_check_at = datetime.now(UTC) + timedelta(minutes=1)
        operation.status, operation.message = "queued", entry.message
        if status != "todo":
            operation.job_id = await enqueue(
                db, "organization.publish", operation_id=str(operation.id)
            )
    db.add(
        AuditEvent(
            actor_id=admin.id, action="organization.import.cancel-requested", entity_id=entry.id
        )
    )
    await db.commit()
    return await view(db, run)


@router.get("/plans/{plan_id}/imports", response_model=list[RunView])
async def plan_imports(plan_id: UUID, admin: Admin, db: Database):
    rows = (
        await db.execute(
            select(ImportRun.id, ImportRun.plan_id, ImportRun.created_at)
            .where(ImportRun.plan_id == plan_id, ImportRun.owner_id == admin.id)
            .order_by(ImportRun.created_at.desc())
            .limit(25)
        )
    ).all()
    return await views(db, rows)


@router.get("/imports/{run_id}", response_model=RunView)
async def import_run(run_id: UUID, admin: Admin, db: Database):
    row = (
        await db.execute(
            select(ImportRun.id, ImportRun.plan_id, ImportRun.created_at).where(
                ImportRun.id == run_id, ImportRun.owner_id == admin.id
            )
        )
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Import not found")
    return await view(db, row)


@router.post("/imports/{run_id}/entries/{entry_id}/retry", response_model=RunView, status_code=202)
async def retry_entry(run_id: UUID, entry_id: UUID, admin: Admin, db: Database):
    await assert_admin(db, admin.id)
    if get_settings().recovery_mode:
        raise HTTPException(409, "Publication is paused for recovery")
    run = await db.scalar(
        select(ImportRun).where(ImportRun.id == run_id, ImportRun.owner_id == admin.id)
    )
    entry = await db.get(ImportEntry, entry_id, with_for_update=True)
    if not run or not entry or entry.run_id != run.id:
        raise HTTPException(404, "Import entry not found")
    if (
        entry.state not in {"held", "awaiting-library"}
        or not entry.reserved
        or not entry.specification
    ):
        raise HTTPException(409, "This entry cannot be retried; review or create a fresh plan")
    operation = await db.get(Operation, entry.operation_id)
    from app.domain.recovery_approvals import require_current

    await require_current(db, "operation", operation.id)
    # Never restart a live job solely because its observation is delayed.
    from sqlalchemy import text

    status = await db.scalar(
        text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
        {"id": operation.job_id},
    )
    if status in {"todo", "doing"}:
        return await view(db, run)
    destination = await db.get(ImportDestination, entry.destination_id)
    current = await destination_configuration(db, destination)
    previous = entry.configuration["destination"]

    # Explicit retry may use a rotated token for the same server/library/paths.
    def without_generation(value):
        return {
            **value,
            "backend": {key: item for key, item in value["backend"].items() if key != "generation"},
        }

    if without_generation(previous) != without_generation(current):
        raise HTTPException(
            409, "The frozen server, library or paths changed; resolve the mapping before retrying"
        )
    entry.configuration = {**entry.configuration, "destination": current}
    entry.state = "awaiting-library" if entry.published_at else "queued"
    entry.message = "Import retry requested"
    operation.status = "queued"
    operation.job_id = await enqueue(db, "organization.publish", operation_id=str(operation.id))
    db.add(AuditEvent(actor_id=admin.id, action="organization.import.retry", entity_id=entry.id))
    await db.commit()
    return await view(db, run)


class BookdropReviewInput(BaseModel):
    action: Literal["refresh", "link", "reject"]
    asset_id: UUID | None = None


class BookdropCandidate(BaseModel):
    id: UUID
    title: str
    library_name: str


async def bookdrop_entry(db, admin, run_id, entry_id):
    from app.db.models import Integration
    from app.domain.recovery_approvals import require_current
    from app.importing.bookdrop import REVIEW_STATES

    await assert_admin(db, admin.id)
    if get_settings().recovery_mode:
        raise HTTPException(409, "Bookdrop review is paused during recovery")
    run = await db.scalar(
        select(ImportRun).where(ImportRun.id == run_id, ImportRun.owner_id == admin.id)
    )
    entry = await db.get(ImportEntry, entry_id, with_for_update=True)
    if not run or not entry or entry.run_id != run.id:
        raise HTTPException(404, "Import entry not found")
    await require_current(db, "import-plan", run.plan_id)
    if (
        entry.state not in REVIEW_STATES
        or not entry.published_at
        or (entry.configuration or {}).get("destination", {}).get("workflow") != "bookdrop"
    ):
        raise HTTPException(409, "This entry is not awaiting Bookdrop review")
    backend = entry.configuration["destination"]["backend"]
    integration = await db.get(Integration, UUID(backend["integration_id"]))
    if (
        not integration
        or integration.deleted_at
        or not integration.enabled
        or integration.kind != "grimmory"
        or integration.base_url != backend["base_url"]
    ):
        raise HTTPException(409, "Restore this handoff's Grimmory connection before reviewing it")
    return run, entry, integration


def bookdrop_candidates_query(entry, integration):
    from app.db.models import Library, LibraryAsset

    return (
        select(LibraryAsset, Library)
        .join(Library)
        .where(
            Library.integration_id == integration.id,
            Library.accessible.is_(True),
            LibraryAsset.version_id == entry.version_id,
            LibraryAsset.medium == "ebook",
            LibraryAsset.state == "present",
            LibraryAsset.full_content.is_(True),
            LibraryAsset.match_status == "matched",
        )
    )


@router.get(
    "/imports/{run_id}/entries/{entry_id}/bookdrop-candidates",
    response_model=list[BookdropCandidate],
)
async def bookdrop_candidates(run_id: UUID, entry_id: UUID, admin: Admin, db: Database):
    _, entry, integration = await bookdrop_entry(db, admin, run_id, entry_id)
    return [
        BookdropCandidate(
            id=asset.id, title=asset.title or "Imported book", library_name=library.name
        )
        for asset, library in (
            await db.execute(bookdrop_candidates_query(entry, integration))
        ).all()
    ]


@router.post("/imports/{run_id}/entries/{entry_id}/bookdrop", response_model=RunView)
async def review_bookdrop(
    run_id: UUID, entry_id: UUID, body: BookdropReviewInput, admin: Admin, db: Database
):
    from app.adapters.contracts import AdapterError
    from app.api.library_folders import library_client
    from app.db.models import LibraryAsset, Version
    from app.importing.bookdrop import observe

    run, entry, integration = await bookdrop_entry(db, admin, run_id, entry_id)
    if body.action == "reject":
        entry.state = "rejected"
        entry.message = (
            "Marked rejected in Dewarr. The delivery receipt is retained to prevent "
            "resending. Manage or discard the intake file in Grimmory."
        )
    elif body.action == "refresh":
        if entry.state == "rejected":
            raise HTTPException(
                409, "Rejected handoffs remain suppressed; link an imported copy to resolve one"
            )
        try:
            async with library_client(integration) as adapter:
                await observe(adapter, entry)
        except (AdapterError, InvalidToken, KeyError, ValueError) as error:
            raise HTTPException(
                422,
                str(error)
                if isinstance(error, AdapterError)
                else "Reconnect Grimmory before reviewing this handoff",
            ) from error
    else:
        if not body.asset_id:
            raise HTTPException(422, "Select the actual library copy after syncing Grimmory")
        row = (
            await db.execute(
                bookdrop_candidates_query(entry, integration)
                .where(LibraryAsset.id == body.asset_id)
                .with_for_update()
            )
        ).first()
        if not row:
            raise HTTPException(
                409,
                (
                    "Sync Grimmory and resolve the imported book's edition before "
                    "linking its complete library copy"
                ),
            )
        asset, library = row
        try:
            async with library_client(integration) as adapter:
                item = await adapter.item(asset.external_id)
        except (AdapterError, InvalidToken, KeyError, ValueError) as error:
            raise HTTPException(
                422,
                str(error)
                if isinstance(error, AdapterError)
                else "Reconnect Grimmory before reviewing this handoff",
            ) from error
        if (
            item.id != asset.external_id
            or item.library_id != library.external_id
            or item.invalid
            or item.unreadable
            or not item.full_ebook
            or item.missing
            or not item.ebook
        ):
            raise HTTPException(
                409, "The library copy changed or is incomplete; sync Grimmory before linking"
            )
        observed = {file.path for file in item.ebook}
        if not {file["path"] for file in asset.files}.issubset(observed):
            raise HTTPException(409, "The library files changed; sync Grimmory before linking")
        entry.asset_id, entry.confirmed_at, entry.state = asset.id, datetime.now(UTC), "confirmed"
        entry.message = "Linked the reviewed copy in " + library.name
        version = await db.get(Version, entry.version_id)
        await enqueue(db, "acquisition.fulfillment", work_id=str(version.work_id))
    entry.next_check_at = entry.run_token = None
    operation = await db.get(Operation, entry.operation_id)
    operation.status, operation.message = "completed", entry.message
    db.add(
        AuditEvent(
            actor_id=admin.id,
            action=f"organization.bookdrop.{body.action}",
            entity_id=entry.id,
            detail={"asset_id": str(entry.asset_id) if entry.asset_id else None},
        )
    )
    await db.commit()
    return await view(db, run)
