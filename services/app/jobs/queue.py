from functools import lru_cache

import procrastinate
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

tasks = procrastinate.Blueprint()


class WorkerApp(procrastinate.App):
    def _register_builtin_tasks(self) -> None:
        from procrastinate import builtin_tasks

        # The pinned library mutates the shared builtin blueprint's namespace on
        # each App construction. Bind a fresh task, retaining both supported names.
        task = self.task(
            name="builtin:procrastinate.builtin_tasks.remove_old_jobs",
            pass_context=True,
            queue="builtin",
        )(builtin_tasks.remove_old_jobs.func)
        self.add_task_alias(task=task, alias="procrastinate.builtin_tasks.remove_old_jobs")


@lru_cache
def get_queue() -> procrastinate.App:
    # Register task definitions before building the queue's task registry.
    from app.jobs import tasks as _tasks  # noqa: F401
    from app.recovery_queue import guard_job

    queue = WorkerApp(
        worker_defaults={"worker_middleware": [guard_job]},
        connector=procrastinate.PsycopgConnector(
            conninfo=get_settings().psycopg_url,
            min_size=1,
            max_size=4,
            kwargs={"options": "-csearch_path=public,book_queue"},
        ),
    )
    queue.add_tasks_from(tasks, namespace="")
    from app.jobs.recovery_tasks import tasks as recovery_tasks

    queue.add_tasks_from(recovery_tasks, namespace="")
    return queue


class RecoveryApp(procrastinate.App):
    def _register_builtin_tasks(self) -> None:
        # Procrastinate 3.9.0 registers history cleanup in every ordinary App.
        # Recovery must preserve that history and permits only explicit recovery tasks.
        # Keep the pinned-version registry/isolation test when upgrading the queue.
        pass


def recovery_queue() -> procrastinate.App:
    from app.jobs.recovery_tasks import tasks as recovery_tasks

    queue = RecoveryApp(
        worker_defaults={"queues": ["recovery"], "concurrency": 1},
        connector=procrastinate.PsycopgConnector(
            conninfo=get_settings().psycopg_url,
            min_size=1,
            max_size=2,
            kwargs={"options": "-csearch_path=public,book_queue"},
        ),
    )
    queue.add_tasks_from(recovery_tasks, namespace="")
    if (
        set(queue.tasks)
        != {
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
        or queue.periodic_registry.periodic_tasks
    ):
        raise RuntimeError("Recovery worker registry contains an unauthorized task")
    return queue


async def enqueue(
    db: AsyncSession, task_name: str, *, schedule_in=None, job_lock=None, job_queue=None, **kwargs
) -> int:
    """Join the caller's psycopg transaction; never commit or open another one."""
    await db.flush()
    connection = await db.connection()
    raw = await connection.get_raw_connection()
    task = get_queue().tasks[task_name]
    return await task.configure(
        connection=raw.driver_connection,
        schedule_in=schedule_in,
        **({"lock": job_lock} if job_lock else {}),
        **({"queue": job_queue} if job_queue else {}),
    ).defer_async(**kwargs)
