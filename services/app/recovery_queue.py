"""Durable restore boundaries for queue IDs and the records jobs can address."""

from uuid import UUID

from procrastinate.exceptions import JobAborted
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db.models import AuditEvent, RecoveryQueueFence, RecoveryQueueSubject, RestoreCheckpoint
from app.db.session import session_factory
from app.recovery import active_restore, restore_pending

SUBJECT_ARGUMENTS = {
    "operation_id": "operation",
    "search_id": "operation",
    "attempt_id": "download-attempt",
    "recovery_id": "download-recovery",
    "automatic_id": "automatic-import",
    "continuation_id": "import-continuation",
    "work_id": "work",
}
# The account that asked for the job, not a restored record. Old job ids stay
# blocked by the queue ceiling; a new scan of the same account may run.
ACTOR_ARGUMENTS = {"user_id"}
RESTORE_HELD_TASK_ARGUMENTS = {
    # Discovery follows use a composite, non-UUID key and have no recovery activation yet.
    "discovery.refresh": {"user_id", "collection_id", "generation"},
}
RECOVERY_TASKS = {
    "recovery.scan",
    "recovery.reconcile",
    "recovery.inventory",
    "recovery.publication",
    "recovery.lists",
    "recovery.outbound",
    "recovery.commands",
    "recovery.access",
    "recovery.connections",
    "recovery.sources",
    "recovery.source-test",
}

# One statement seals both the high-water mark and exact subject membership.
# The caller must be offline or hold the active restore pause with no ordinary workers.
SEAL_SQL = """
WITH checkpoint AS (
 SELECT id FROM restore_checkpoints WHERE id=:checkpoint_id AND active FOR UPDATE
), subjects AS (
 SELECT 'operation' AS kind, id AS subject_id FROM operations WHERE kind NOT LIKE 'recovery.%'
 UNION ALL SELECT 'download-attempt',id FROM download_attempts
 UNION ALL SELECT 'download-recovery',id FROM download_recoveries
 UNION ALL SELECT 'automatic-import',id FROM automatic_imports
 UNION ALL SELECT 'import-continuation',id FROM automatic_import_continuations
 UNION ALL SELECT 'work',id FROM works
 UNION ALL SELECT 'selection',id FROM acquisition_selections
 UNION ALL SELECT 'import-plan',id FROM frozen_import_plans
 UNION ALL SELECT 'csv-preview',id FROM list_csv_imports
), inserted AS (
 INSERT INTO recovery_queue_subjects(checkpoint_id,kind,subject_id)
 SELECT c.id,s.kind,s.subject_id FROM checkpoint c CROSS JOIN subjects s
 WHERE NOT EXISTS(SELECT 1 FROM recovery_queue_fences f WHERE f.checkpoint_id=c.id)
 ON CONFLICT DO NOTHING RETURNING kind
), counts AS (
 SELECT kind,count(*) AS total FROM inserted GROUP BY kind
)
INSERT INTO recovery_queue_fences(
 checkpoint_id,job_id_through,job_count,subject_counts,approval_version
)
SELECT c.id,(SELECT coalesce(max(id),0) FROM book_queue.procrastinate_jobs),
 (SELECT count(*) FROM book_queue.procrastinate_jobs),
 coalesce((SELECT jsonb_object_agg(kind,total) FROM counts),'{}'::jsonb),1
FROM checkpoint c ON CONFLICT DO NOTHING
"""


async def seal(db, checkpoint_id):
    await db.execute(text(SEAL_SQL), {"checkpoint_id": checkpoint_id})


async def denial(db, job):
    checkpoint = await active_restore(db)
    paused = get_settings().recovery_mode or await restore_pending(db)
    if job.task_name in RECOVERY_TASKS:
        return (
            None if checkpoint and checkpoint.active else "Recovery requires an active checkpoint"
        )
    if paused:
        return "Ordinary work is paused for restore review"
    missing = await db.scalar(
        select(RestoreCheckpoint.id)
        .where(
            ~select(RecoveryQueueFence.checkpoint_id)
            .where(RecoveryQueueFence.checkpoint_id == RestoreCheckpoint.id)
            .exists()
        )
        .limit(1)
    )
    if missing:
        return "Restore history has no sealed queue boundary; recovery review is required"
    incomplete = await db.scalar(
        select(RecoveryQueueFence.checkpoint_id)
        .where(RecoveryQueueFence.approval_version != 1)
        .limit(1)
    )
    if incomplete:
        return "Restore history has no complete approval boundary; recovery review is required"
    ceiling = await db.scalar(select(func.max(RecoveryQueueFence.job_id_through)))
    if ceiling is None:
        return None
    if job.task_name in RESTORE_HELD_TASK_ARGUMENTS:
        return "Discovery tracking requires fresh recovery activation after restore"
    if not isinstance(job.id, int) or job.id <= ceiling:
        return "This job belongs to the restored queue and cannot be replayed"
    if job.task_name.startswith(("procrastinate.", "builtin:")):
        return "Queue history cleanup remains held after restore"
    references = []
    for name, value in job.task_kwargs.items():
        if name == "operation_ids":
            if (
                job.task_name != "organization.confirm-batch"
                or not isinstance(value, list)
                or not 1 <= len(value) <= 20
            ):
                return "This job reference is invalid"
            references.extend(("operation", identifier) for identifier in value)
            continue
        if not name.endswith("_id") or name in ACTOR_ARGUMENTS:
            continue
        kind = SUBJECT_ARGUMENTS.get(name)
        if not kind:
            return "This job reference has no supported restore fence"
        references.append((kind, value))
    for kind, value in references:
        try:
            identifier = UUID(value)
        except (ValueError, TypeError, AttributeError):
            return "This job reference is invalid"
        historical = await db.scalar(
            select(RecoveryQueueSubject.checkpoint_id)
            .where(RecoveryQueueSubject.kind == kind, RecoveryQueueSubject.subject_id == identifier)
            .limit(1)
        )
        if historical:
            return "This job references restored state; fresh recovery activation is required"
    return None


async def guard_job(call_next, context, worker):
    async with session_factory()() as db, db.begin():
        reason = await denial(db, context.job)
        if reason:
            # Never log task arguments: they can contain private references or tokens.
            db.add(
                AuditEvent(
                    action="recovery.queue.blocked",
                    detail={
                        "job_id": context.job.id,
                        "task": context.job.task_name,
                        "reason": reason,
                    },
                )
            )
    if reason:
        raise JobAborted(reason)
    return await call_next()
