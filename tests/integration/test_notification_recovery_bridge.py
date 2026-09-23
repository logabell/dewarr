# ruff: noqa: F811, F401
import pytest
from sqlalchemy import func, select

from app.db.models import DownloadRecovery, NotificationEvent
from app.domain import download_recovery
from app.notifications.delivery import tick
from tests.integration.test_acquisition import catalog
from tests.integration.test_acquisition_selections import selection_route
from tests.integration.test_automatic_selection import source
from tests.integration.test_failed_download_recovery import failed_source
from tests.integration.test_notifications import channel, sent

pytestmark = pytest.mark.integration


async def test_recovery_bridge_commits_deduplicated_deliveries_and_discards_rollback(
    client, database, failed_source, sent
):
    saved = await channel(client, ["download.stalled", "download.retried", "download.gave_up"])
    recovery_id = failed_source["recovery_id"]
    prefix = f"recovery:{recovery_id}:"
    async with database() as db, db.begin():
        row = await db.get(DownloadRecovery, recovery_id)
        assert (
            await db.scalar(
                select(func.count())
                .select_from(NotificationEvent)
                .where(NotificationEvent.key == prefix + "download.stalled")
            )
            == 1
        )
        await download_recovery.event(db, row, "retried")
        await download_recovery.event(db, row, "retried")
    async with database() as db:
        row = await db.get(DownloadRecovery, recovery_id)
        await download_recovery.event(db, row, "gave-up")
        await db.rollback()
    await tick()
    await tick()
    assert len(sent) == 1
    assert len(sent[0]["events"]) == 1
    assert sent[0]["events"][0]["type"] == "download.retried"
    assert sent[0]["events"][0]["url"].endswith("/requests#downloads")
    history = (
        await client.get("/api/notifications/channels/" + saved["id"] + "/deliveries")
    ).json()
    assert len(history) == 1 and history[0]["state"] == "sent"
    async with database() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(NotificationEvent)
                .where(NotificationEvent.key == prefix + "download.gave_up")
            )
            == 0
        )
