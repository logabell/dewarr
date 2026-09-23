import argparse
import asyncio
import logging

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
                "library.match",
                "library.combine",
                "metadata.resolve-import",
                "acquisition.evaluate",
                "acquisition.download",
                "acquisition.downloads.schedule",
                "acquisition.reconcile",
                "acquisition.fulfillment",
                "organization.inspect",
                "organization.automatic",
                "organization.reuse",
                "organization.probe",
                "organization.publish",
                "organization.confirm",
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
        try:
            await queue.run_worker_async(
                concurrency=4,
                update_heartbeat_interval=10,
                stalled_worker_timeout=60,
            )
        finally:
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--recovery", action="store_true", help="Run only restricted restore recovery tasks"
    )
    asyncio.run(main(recovery_only=parser.parse_args().recovery))
