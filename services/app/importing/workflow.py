import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db.models import AuditEvent, DownloadInspection, Operation, User
from app.db.session import session_factory
from app.domain.download_reviews import validate_inspection
from app.domain.operations import transaction_lock
from app.importing.filesystem import InspectionError
from app.importing.inspection import inspect_download
from app.importing.storage import import_sources


def source_matches(row, sources):
    return sources.get(row.source_key) == Path(row.source_path)


async def run_inspection(operation_id: UUID):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        identifier = await db.scalar(
            select(DownloadInspection.id).where(DownloadInspection.operation_id == operation_id)
        )
        if not identifier:
            return
        await transaction_lock(db, f"inspection-plan:{identifier}")
        row = await db.scalar(
            select(DownloadInspection)
            .where(DownloadInspection.operation_id == operation_id)
            .with_for_update()
        )
        if not row or row.state in {"ready", "failed"}:
            return
        operation = await db.get(Operation, operation_id)
        try:
            await validate_inspection(db, row.id)
        except HTTPException as error:
            row.state, operation.status = "failed", "failed"
            row.message = operation.message = str(error.detail)
            return
        actor = await db.get(User, row.owner_id)
        if (
            get_settings().recovery_mode
            or not actor.active
            or actor.role != "admin"
            or not source_matches(row, await import_sources(db))
        ):
            row.state, operation.status = "failed", "failed"
            row.message = operation.message = (
                "Inspection paused: access or download root configuration changed"
            )
            return
        row.run_token, row.state, operation.status = token, "running", "running"
        row.message = operation.message = "Inspecting file contents without modifying downloads"
        path, relative = Path(row.source_path), row.relative_path
        selected_paths = operation.payload.get("selected_paths")
    try:
        snapshot = await asyncio.to_thread(
            inspect_download, path, relative, selected_paths=selected_paths
        )
        message = "Files inspected; match groups to catalog versions before planning an import"
    except (OSError, ValueError) as error:
        snapshot = None
        message = (
            str(error)[:300]
            if isinstance(error, InspectionError)
            else ("Inspection could not read a stable download tree; check the worker mount")
        )
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"inspection-plan:{identifier}")
        row = await db.scalar(
            select(DownloadInspection)
            .where(DownloadInspection.operation_id == operation_id)
            .with_for_update()
        )
        if not row or row.run_token != token or row.state != "running":
            return
        actor = await db.get(User, row.owner_id, populate_existing=True)
        try:
            await validate_inspection(db, row.id)
        except HTTPException as error:
            snapshot, message = None, str(error.detail)
        if (
            get_settings().recovery_mode
            or not actor.active
            or actor.role != "admin"
            or not source_matches(row, await import_sources(db))
        ):
            snapshot, message = None, "Inspection access changed; no results were published"
        row.snapshot, row.message = snapshot, message
        row.state = "ready" if snapshot is not None else "failed"
        row.run_token = None
        operation = await db.get(Operation, operation_id)
        operation.status = "completed" if snapshot is not None else "failed"
        operation.message = message
        from app.importing.automatic import continue_inspection

        await continue_inspection(db, row.id)
        db.add(
            AuditEvent(
                actor_id=row.owner_id,
                action="organization.inspected",
                entity_id=row.id,
                detail={"state": row.state},
            )
        )
