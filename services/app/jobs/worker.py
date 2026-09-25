import argparse
import asyncio
import logging
import signal

from app.config import get_settings
from app.db.session import get_engine, session_factory
from app.jobs.queue import get_queue, recovery_queue
from app.recovery import active_restore, restore_pending, runtime_lease

logger = logging.getLogger(__name__)


async def recover_observation_jobs(queue):
    while True:
        try:
            for task_name in (
                "recovery.scan",
                "recovery.reconcile",
                "recovery.inventory",
                "recovery.publication",
                "recovery.lists",
                "recovery.outbound",
                "recovery.commands",
            ):
                for job in await queue.job_manager.get_stalled_jobs(
                    task_name=task_name, seconds_since_heartbeat=60
                ):
                    await queue.job_manager.retry_job(job)
        except Exception as error:
            logger.error("Recovery observation queue unavailable (%s)", type(error).__name__)
        await asyncio.sleep(30)


async def recover_stalled_jobs() -> None:
    queue = get_queue()
    while True:
        # Each side-effecting workflow must supply its own reconciliation path.
        # These jobs are idempotent diagnostics or fenced, read-only inventory workflows.
        try:
            for task_name in (
                "system.probe",
                "connections.health",
                "notifications.dispatch",
                "sources.search",
                "sources.prepare",
                "catalog.series.refresh",
                "series.requests",
                "series.acquire",
                "acquisition.auto-select",
                "acquisition.quick-add",
                "acquisition.pack-dispatch",
                "lists.sync",
                "discovery.schedule",
                "releases.schedule",
                "discovery.refresh",
                "lists.writeback",
                "lists.writeback.compare",
                "lists.csv",
                "lists.requests",
                "lists.schedule",
                "lists.acquire",
                "lists.acquisition.schedule",
                "library.sync",
                "library.schedule",
                "metadata.enrich",
                "catalog.refresh",
                "library.match",
                "library.combine",
                "metadata.resolve-import",
                "acquisition.evaluate",
                "acquisition.download",
                "acquisition.recover-download",
                "acquisition.reject-download",
                "acquisition.downloads.schedule",
                "acquisition.reconcile",
                "acquisition.fulfillment",
                "organization.inspect",
                "organization.automatic",
                "organization.reuse",
                "organization.probe",
                "organization.publish",
                "organization.confirm",
                "organization.confirm-batch",
            ):
                stalled = await queue.job_manager.get_stalled_jobs(
                    task_name=task_name,
                    seconds_since_heartbeat=60,
                )
                for job in stalled:
                    await queue.job_manager.retry_job(job)
        except Exception as error:
            # A temporary DB outage must not silently stop the recovery loop.
            logger.error("Stalled-job recovery unavailable (%s)", type(error).__name__)
        await asyncio.sleep(30)


async def main(*, recovery_only=False) -> None:
    settings = get_settings()
    settings.encryption_key()
    if settings.recovery_mode and not recovery_only:
        raise RuntimeError("Workers are disabled in recovery mode; reconcile before resuming")
    try:
        async with runtime_lease():
            async with session_factory()() as db:
                if recovery_only:
                    if not await active_restore(db):
                        raise RuntimeError(
                            "Recovery observations require an active restore checkpoint"
                        )
                    settings.recovery_mode = True
                elif await restore_pending(db):
                    raise RuntimeError(
                        "Restored state requires reconciliation before workers can resume"
                    )
            if recovery_only:
                queue = recovery_queue()
                async with queue.open_async():
                    recovery = asyncio.create_task(recover_observation_jobs(queue))
                    try:
                        await queue.run_worker_async(queues=["recovery"], concurrency=1)
                    finally:
                        recovery.cancel()
                        await asyncio.gather(recovery, return_exceptions=True)
            else:
                await run_worker()
    finally:
        await get_engine().dispose()


async def run_worker() -> None:
    queue = get_queue()
    async with queue.open_async():
        recovery = asyncio.create_task(recover_stalled_jobs())
        loop, main_task = asyncio.get_running_loop(), asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
        try:
            await run_pools(queue)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)


async def run_pools(queue):
    control = {"system", "notifications"}
    files = {"imports", "inspection"}
    inventory = {"inventory"}
    confirmation = {"confirmation"}
    catalog_cache = {"catalog-cache"}
    remaining = (
        {task.queue for task in queue.tasks.values()}
        - control
        - files
        - inventory
        - confirmation
        - catalog_cache
        - {"recovery"}
    )
    # One slot per work class. Confirmation may hash large files, so it
    # must not share the slot responsible for scheduling and health.
    async with asyncio.TaskGroup() as workers:
        for index, queues in enumerate(
            (control, files, inventory, confirmation, remaining, catalog_cache)
        ):
            workers.create_task(
                queue.run_worker_async(
                    queues=sorted(queues),
                    concurrency=1,
                    name=(
                        "control",
                        "files",
                        "inventory",
                        "confirmation",
                        "background",
                        "catalog-cache",
                    )[index],
                    install_signal_handlers=False,
                    listen_notify=index == 0,
                    fetch_job_polling_interval=1,
                    update_heartbeat_interval=10,
                    stalled_worker_timeout=60,
                )
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--recovery", action="store_true", help="Run only restricted restore recovery tasks"
    )
    asyncio.run(main(recovery_only=parser.parse_args().recovery))
