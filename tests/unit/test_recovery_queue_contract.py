import inspect

from app.jobs.queue import get_queue, recovery_queue
from app.recovery_queue import (
    ACTOR_ARGUMENTS,
    RECOVERY_TASKS,
    RESTORE_HELD_TASK_ARGUMENTS,
    SUBJECT_ARGUMENTS,
    guard_job,
)


def test_worker_fence_covers_all_registered_record_arguments():
    queue = get_queue()
    assert queue.worker_defaults["worker_middleware"] == [guard_job]
    assert set(recovery_queue().tasks) == RECOVERY_TASKS
    allowed = set(SUBJECT_ARGUMENTS) | set(ACTOR_ARGUMENTS) | {"timestamp", "source"}
    for name, task in queue.tasks.items():
        if name in RECOVERY_TASKS or name.startswith(("procrastinate.", "builtin:")):
            continue
        parameters = set(inspect.signature(task.func).parameters)
        if task.pass_context:
            # Injected by Procrastinate, not a serialized job reference.
            parameters.remove(next(iter(inspect.signature(task.func).parameters)))
        if name == "organization.confirm-batch":
            assert parameters == {"operation_ids"}
        elif name in RESTORE_HELD_TASK_ARGUMENTS:
            assert parameters == RESTORE_HELD_TASK_ARGUMENTS[name], name
        else:
            assert parameters <= allowed, name


def test_builtin_cleanup_names_do_not_accumulate_across_queue_construction():
    from app.jobs.queue import WorkerApp

    expected = {
        "builtin:procrastinate.builtin_tasks.remove_old_jobs",
        "procrastinate.builtin_tasks.remove_old_jobs",
    }
    for _ in range(20):
        queue = WorkerApp(connector=get_queue().connector)
        assert set(queue.tasks) == expected
        assert (
            queue.tasks["procrastinate.builtin_tasks.remove_old_jobs"].name
            == "builtin:procrastinate.builtin_tasks.remove_old_jobs"
        )
