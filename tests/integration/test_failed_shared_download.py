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


async def test_peer_refusal_excludes_only_the_peer_without_global_release_block(
    client, database, selected, second
):
    reply = await grouped(client, selected, second)
    assert reply.status_code == 202, reply.text
    source_id = "a" * 40
    async with database() as db, db.begin():
        attempt, _ = await download_attempts.locked(db, UUID(reply.json()["id"]))
        for selection_id in (UUID(selected["id"]), UUID(second["id"])):
            selection = await db.get(AcquisitionSelection, selection_id)
            selection.frozen = {
                **selection.frozen,
                "automatic_selection": {"excluded_source_ids": ["b" * 40]},
            }
        recoveries = await download_recovery.peer_refused(
            db, attempt, "Soulseek peer refused the file: File not shared", source_id
        )
        assert len(recoveries) == 2
        assert all(
            r.evidence["excluded_source_ids"] == sorted(["a" * 40, "b" * 40]) for r in recoveries
        )
        assert all("excluding this Soulseek folder" in r.message for r in recoveries)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(ReleaseBlock)) == 0
        assert await db.scalar(select(func.count()).select_from(DownloadRecovery)) == 2


async def test_confirmed_peer_refusal_bypasses_the_generic_error_grace(
    client, database, selected, monkeypatch
):
    from app.adapters.slskd import SlskdState
    from app.domain import capacity
    from app.domain.download_recovery import RecoveryConfiguration

    reply = await client.post(
        "/api/acquisition/downloads",
        headers={"Idempotency-Key": "peer-refusal-immediate-command"},
        json={"selection_id": selected["id"]},
    )
    assert reply.status_code == 202, reply.text
    calls = []

    async def configuration(_db):
        return RecoveryConfiguration()

    async def peer_refused(_db, _attempt, reason, source_id):
        calls.append((reason, source_id))

    async def release_slot(_db, _attempt):
        calls.append("released")

    def no_grace(*_args, **_kwargs):
        raise AssertionError("Confirmed peer refusal must not enter generic error grace")

    monkeypatch.setattr(download_recovery, "configuration", configuration)
    monkeypatch.setattr(download_recovery, "peer_refused", peer_refused)
    monkeypatch.setattr(download_recovery, "observe", no_grace)
    monkeypatch.setattr(capacity, "release_slot", release_slot)

    async with database() as db, db.begin():
        attempt, _ = await download_attempts.locked(db, UUID(reply.json()["id"]))
        selection = await db.get(AcquisitionSelection, attempt.selection_id)
        selection.frozen = {
            **selection.frozen,
            "release": {"source": "slskd", "source_id": "c" * 40},
        }
        state = SlskdState(
            external_id=str(attempt.id),
            state="failed",
            completed=False,
            save_path="",
            association_verified=True,
            failure_reason="Transfer rejected: File not shared.",
            peer_refusal=True,
        )
        await download_attempts.finish_observation(db, attempt, selection, state)

    assert calls == [
        ("Soulseek peer refused the file: Transfer rejected: File not shared.", "c" * 40),
        "released",
    ]
