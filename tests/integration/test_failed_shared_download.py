# ruff: noqa: F811
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.models import AcquisitionSelection, DownloadRecovery, ReleaseBlock
from app.domain import download_attempts, download_recovery
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_download_attempts import selected  # noqa: F401
from tests.integration.test_shared_downloads import grouped, second  # noqa: F401

pytestmark = pytest.mark.integration


async def test_failed_shared_transfer_has_one_independent_recovery_for_each_member(
    client, database, selected, second
):
    reply = await grouped(client, selected, second)
    assert reply.status_code == 202, reply.text
    async with database() as db, db.begin():
        attempt, _ = await download_attempts.locked(db, UUID(reply.json()["id"]))
        recoveries = await download_recovery.failed(db, attempt, "Downloader reported failed")
        assert len(recoveries) == 2
        assert {r.selection_id for r in recoveries} == {UUID(selected["id"]), UUID(second["id"])}
        assert len({r.job_id for r in recoveries}) == 2
        assert {r.root_selection_id for r in recoveries} == {r.selection_id for r in recoveries}
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(ReleaseBlock)) == 2
        assert await db.scalar(select(func.count()).select_from(DownloadRecovery)) == 2
        assert all(s.state == "cancelled" for s in await db.scalars(select(AcquisitionSelection)))
