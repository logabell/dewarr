"""Durable cancellation reconciles publication before releasing its reservation."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.db.models import AuditEvent, ImportDestination, ImportEntry, ImportRun, Operation, User
from app.db.session import session_factory
from app.importing.cancel_files import cancel_files
from app.importing.execution import RenameGuard, Superseded
from app.importing.publication import PublicationBusy, PublicationError, PublicationSpec
from app.importing.storage import frozen_storage_matches, storage_settings
from app.jobs.queue import enqueue


class CancellationGuard(RenameGuard):
    async def enter(self):
        self.db = session_factory()()
        try:
            await self.db.begin()
            entry = await self.db.get(ImportEntry, self.entry_id)
            run = await self.db.get(ImportRun, entry.run_id)
            actor = await self.db.get(
                User, run.owner_id, with_for_update={"read": True}, populate_existing=True
            )
            destination = await self.db.get(
                ImportDestination, entry.destination_id, with_for_update=True
            )
            await self.db.refresh(entry, with_for_update=True)
            if entry.run_token != self.token or entry.state != "cancelling":
                raise Superseded("A newer attempt owns this cancellation")
            settings = await storage_settings(self.db)
            if settings.recovery_mode or not actor or not actor.active or actor.role != "admin":
                raise PublicationError("Cancellation requires active administrator access")
            spec = PublicationSpec.model_validate(entry.specification)
            if (
                not destination
                or settings.import_destinations.get(destination.root_key) != spec.destination_root
                or not frozen_storage_matches(settings, destination.root_key, spec)
            ):
                raise PublicationError(
                    "Restore the frozen library and staging paths before cancelling"
                )
        except BaseException:
            await self.leave()
            raise


async def execute(operation_id: UUID, *, checkpoint=lambda _: None):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id)
        if not operation or operation.kind != "organization.publish":
            return
        entry = await db.get(ImportEntry, UUID(operation.payload["entry_id"]), with_for_update=True)
        if entry.state != "cancelling":
            return
        entry.run_token = token
        entry_id, owner_id = entry.id, operation.owner_id
        spec = PublicationSpec.model_validate(entry.specification)
        operation.status, operation.message = "running", "Checking publication before cancellation"
    try:
        if spec.mode == "rename":
            from app.importing.seeding_rename import undo_unplaced_rename

            await undo_unplaced_rename(entry_id, spec)
        guard = CancellationGuard(asyncio.get_running_loop(), entry_id, token)
        task = asyncio.create_task(
            asyncio.to_thread(cancel_files, spec, guard=guard.hold, checkpoint=checkpoint)
        )
        try:
            receipt = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        checkpoint("cancel-before-database")
        async with session_factory()() as db, db.begin():
            entry = await db.get(ImportEntry, entry_id, with_for_update=True)
            if entry.run_token != token or entry.state != "cancelling":
                return
            operation = await db.get(Operation, operation_id)
            entry.receipt, entry.run_token = receipt, None
            if receipt["state"] == "published":
                entry.published_at = entry.published_at or datetime.now(UTC)
                entry.state = "awaiting-library"
                entry.message = (
                    "Book was already published; preserving files and continuing library detection"
                )
                entry.next_check_at = datetime.now(UTC) + timedelta(minutes=1)
                operation.status = "queued"
                operation.job_id = await enqueue(
                    db, "organization.publish", operation_id=str(operation_id)
                )
            else:
                entry.state, entry.reserved, entry.next_check_at = "cancelled", False, None
                entry.message = (
                    "Import stopped; downloaded files are unchanged. You can review a new plan."
                )
                operation.status = "completed"
            operation.message = entry.message
            from app.domain.capacity import release_import

            await release_import(db, entry)
            db.add(
                AuditEvent(
                    actor_id=owner_id,
                    action="organization.import.cancellation-resolved",
                    entity_id=entry_id,
                    detail={"outcome": entry.state},
                )
            )
    except Superseded:
        return
    except PublicationBusy:
        raise  # Durable queue retry; the reservation remains held.
    except (PublicationError, OSError, ValueError, KeyError) as error:
        async with session_factory()() as db, db.begin():
            entry = await db.get(ImportEntry, entry_id, with_for_update=True)
            if entry.run_token != token or entry.state != "cancelling":
                return
            entry.state, entry.run_token, entry.next_check_at = "cancel-held", None, None
            entry.message = (
                str(error)[:500]
                if isinstance(error, PublicationError)
                else "Cancellation needs review of the frozen paths and publication journal"
            )
            operation = await db.get(Operation, operation_id)
            operation.status, operation.message = "failed", entry.message
