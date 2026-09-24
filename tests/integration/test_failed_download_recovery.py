# ruff: noqa: F811
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import (
    AcquisitionSelection,
    DownloadAttempt,
    DownloadIdentityClaim,
    DownloadRecovery,
    DownloadRecoverySettings,
    Operation,
    ReleaseBlock,
)
from app.domain import automatic_selection, download_attempts, download_recovery
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_automatic_selection import (  # noqa: F401
    additional_candidate,
    source,
    start,
)
from tests.integration.test_download_attempts import Client

pytestmark = pytest.mark.integration


@pytest.fixture
async def failed_source(client, database, source, monkeypatch):
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    operation = await start(client, source)
    await automatic_selection.run(UUID(operation["id"]))
    async with database() as db:
        selected_id = UUID((await db.get(Operation, UUID(operation["id"]))).payload["selection_id"])
    response = await client.post(
        "/api/acquisition/downloads",
        json={"selection_id": str(selected_id)},
        headers={"Idempotency-Key": "first-recovery-download"},
    )
    assert response.status_code == 202, response.text
    attempt_id = UUID(response.json()["id"])
    client_mock = Client(database, source["descriptor"].model_dump(mode="json"))
    monkeypatch.setattr(download_attempts, "QbitClient", lambda *args: client_mock)
    await download_attempts.run(attempt_id)
    client_mock.states[0].seeders = 0
    async with database() as db, db.begin():
        db.add(
            DownloadRecoverySettings(id=1, configuration={"sources": {"mam": {"stall_hours": 1}}})
        )
        attempt = await db.get(DownloadAttempt, attempt_id)
        attempt.recovery_observation = {
            "progress": 0.25,
            "progress_since": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "zero_seeders_since": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        }
    await download_attempts.run(attempt_id)
    async with database() as db:
        recovery = await db.scalar(
            select(DownloadRecovery).where(DownloadRecovery.attempt_id == attempt_id)
        )
        assert recovery, (await db.get(DownloadAttempt, attempt_id)).message
        return {
            **source,
            "attempt_id": attempt_id,
            "selection_id": selected_id,
            "recovery_id": recovery.id,
            "client": client_mock,
        }


async def test_stall_blocklists_and_redelivery_dispatches_exactly_one_replacement(
    client, database, failed_source, monkeypatch
):
    old = failed_source
    # A different media container establishes a different torrent identity.
    result_id, artifact_id, release = await additional_candidate(
        database, old, source_id="502", format="mp3"
    )

    async def resolve(owner, row, **kwargs):
        assert row.id == result_id
        return artifact_id, release

    monkeypatch.setattr(automatic_selection, "resolve_candidate", resolve)
    await asyncio.gather(
        download_recovery.run(old["recovery_id"]), download_recovery.run(old["recovery_id"])
    )
    async with database() as db:
        recovery = await db.get(DownloadRecovery, old["recovery_id"])
        assert recovery.state == "selecting", recovery.message
        replacement_id = recovery.replacement_id
    await automatic_selection.run(replacement_id)
    await asyncio.gather(
        download_recovery.run(old["recovery_id"]), download_recovery.run(old["recovery_id"])
    )
    async with database() as db:
        recovery = await db.get(DownloadRecovery, old["recovery_id"])
        assert recovery.state == "retried", recovery.message
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 2
        assert await db.scalar(select(func.count()).select_from(ReleaseBlock)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(DownloadIdentityClaim)
                .where(DownloadIdentityClaim.active.is_(True))
            )
            == 2
        )
        new = await db.get(DownloadAttempt, UUID(recovery.evidence["replacement_attempt_id"]))
        chosen = await db.get(AcquisitionSelection, new.selection_id)
        original = await db.get(AcquisitionSelection, old["selection_id"])
        assert chosen.artifact_id != original.artifact_id
        assert chosen.frozen["requirements"] == original.frozen["requirements"]
        assert chosen.frozen["download_recovery"]["root_selection_id"] == str(original.id)
    await download_attempts.run(old["attempt_id"])
    assert old["client"].calls.count("submit") == 1
    detail = await client.get(f"/api/acquisition/downloads/{old['attempt_id']}")
    assert len(detail.json()["attempt_chain"]) == 2
    assert detail.json()["can_recheck"] is False


async def test_attempt_cap_holds_with_durable_reasons(client, database, failed_source):
    async with database() as db, db.begin():
        settings = await db.get(DownloadRecoverySettings, 1)
        settings.configuration = {**settings.configuration, "attempt_cap": 1}
    await download_recovery.run(failed_source["recovery_id"])
    async with database() as db:
        recovery = await db.get(DownloadRecovery, failed_source["recovery_id"])
        assert recovery.state == "held" and "Gave up after 1" in recovery.message
        assert len(recovery.evidence["attempts"]) == 1
        assert not recovery.replacement_id
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1


async def test_blocklist_removal_retains_claims(client, database, failed_source):
    response = await client.get("/api/acquisition/recovery/blocklist")
    assert response.status_code == 200 and len(response.json()) == 1
    block = response.json()[0]
    assert "No seeders" in block["reason"]
    response = await client.delete(f"/api/acquisition/recovery/blocklist/{block['id']}")
    assert response.status_code == 204
    async with database() as db:
        assert not (await db.get(ReleaseBlock, UUID(block["id"]))).active
        assert (
            await db.scalar(
                select(func.count())
                .select_from(DownloadIdentityClaim)
                .where(DownloadIdentityClaim.active.is_(True))
            )
            == 1
        )


async def test_uncertain_attempt_must_reconcile_before_any_block(
    client, database, source, monkeypatch
):
    from app.adapters.contracts import AdapterError, FailureKind

    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    operation = await start(client, source)
    await automatic_selection.run(UUID(operation["id"]))
    async with database() as db:
        selection_id = (await db.get(Operation, UUID(operation["id"]))).payload["selection_id"]
    response = await client.post(
        "/api/acquisition/downloads",
        json={"selection_id": selection_id},
        headers={"Idempotency-Key": "uncertain-recovery-test"},
    )
    mock = Client(database, source["descriptor"].model_dump(mode="json"))
    mock.fail = AdapterError(FailureKind.UNCERTAIN, "Lost response")
    monkeypatch.setattr(download_attempts, "QbitClient", lambda *args: mock)
    attempt_id = UUID(response.json()["id"])
    await download_attempts.run(attempt_id)
    mock.states = []
    async with database() as db, db.begin():
        attempt = await db.get(DownloadAttempt, attempt_id)
        attempt.recovery_observation = {
            "zero_seeders_since": (datetime.now(UTC) - timedelta(days=10)).isoformat()
        }
    await download_attempts.run(attempt_id)
    async with database() as db:
        attempt = await db.get(DownloadAttempt, attempt_id)
        assert attempt.state == "uncertain" and attempt.recovery_observation == {}
        assert not await db.scalar(select(ReleaseBlock.id))
        assert not await db.scalar(select(DownloadRecovery.id))
    assert mock.calls.count("submit") == 1
    report = await client.post(
        "/api/acquisition/recovery/reports",
        json={"selection_id": selection_id, "reason": "wrong-book"},
    )
    assert report.status_code == 409


async def test_report_requires_approval_and_is_idempotent(client, database, source, monkeypatch):
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    operation = await start(client, source)
    await automatic_selection.run(UUID(operation["id"]))
    async with database() as db:
        selection_id = (await db.get(Operation, UUID(operation["id"]))).payload["selection_id"]
    response = await client.post(
        "/api/acquisition/downloads",
        json={"selection_id": selection_id},
        headers={"Idempotency-Key": "report-problem-test"},
    )
    mock = Client(database, source["descriptor"].model_dump(mode="json"))
    mock.complete = True
    monkeypatch.setattr(download_attempts, "QbitClient", lambda *args: mock)
    await download_attempts.run(UUID(response.json()["id"]))
    command = {"selection_id": selection_id, "reason": "missing-chapters", "require_approval": True}
    replies = await asyncio.gather(
        *(client.post("/api/acquisition/recovery/reports", json=command) for _ in range(2))
    )
    assert all(reply.status_code == 202 for reply in replies), [r.text for r in replies]
    identifier = UUID(replies[0].json()[0]["id"])
    assert replies[0].json()[0]["state"] == "approval"
    assert replies[1].json()[0]["id"] == str(identifier)
    approvals = await client.get("/api/acquisition/recovery/approvals")
    assert [item["id"] for item in approvals.json()] == [str(identifier)]
    async with database() as db:
        selected = await db.get(AcquisitionSelection, UUID(selection_id))
        intent_id = selected.intent_id
    card = (await client.get(f"/api/requests/{intent_id}")).json()
    assert card["targets"][0]["attempt_id"] == response.json()["id"]
    assert card["targets"][0]["can_view_download_history"]
    assert not card["targets"][0]["can_recheck"]
    await download_recovery.run(identifier)
    async with database() as db:
        row = await db.get(DownloadRecovery, identifier)
        assert row.state == "approval" and row.replacement_id is None and row.job_id is None
        assert row.evidence["cleanup_state"] == "pending"
    approved = await client.post(f"/api/acquisition/recovery/{identifier}/approve")
    assert approved.status_code == 202 and approved.json()["state"] == "queued"


async def test_frozen_formats_are_not_widened_by_new_preferences(
    database, failed_source, monkeypatch
):
    # The old selection permitted only M4B. A new profile allowing MP3 cannot widen it.
    async with database() as db, db.begin():
        original = await db.get(AcquisitionSelection, failed_source["selection_id"])
        profile = original.frozen["profile"]
        original.frozen = {
            **original.frozen,
            "profile": {
                **profile,
                "preferences": {
                    **profile["preferences"],
                    "audio_formats": ["m4b"],
                    "blocked_formats": ["mp3"],
                },
            },
        }
    await additional_candidate(database, failed_source, source_id="502", format="mp3")
    await download_recovery.run(failed_source["recovery_id"])
    async with database() as db:
        row = await db.get(DownloadRecovery, failed_source["recovery_id"])
        identifier = row.replacement_id
        assert identifier, row.message
    await automatic_selection.run(identifier)
    async with database() as db:
        operation = await db.get(Operation, identifier)
        assert operation.status == "held" and not operation.payload["selection_id"]
        assert all(item["reasons"] for item in operation.payload["decisions"])
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1


async def test_stale_search_refreshes_once_with_request_binding(
    database, failed_source, monkeypatch
):
    from app.domain import book_sources

    seen = []

    async def search(db, user, work_id, body, key):
        seen.append((body, key))
        operation = Operation(
            owner_id=user.id,
            kind="sources.search",
            idempotency_key=key,
            status="running",
            payload={},
        )
        db.add(operation)
        await db.flush()
        return operation

    monkeypatch.setattr(book_sources, "start", search)
    async with database() as db, db.begin():
        saved = await db.get(Operation, failed_source["search"])
        saved.payload = {
            **saved.payload,
            "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
    await download_recovery.run(failed_source["recovery_id"])
    await download_recovery.run(failed_source["recovery_id"])
    assert len(seen) == 1 and seen[0][0].medium == "audio"
    assert str(seen[0][0].request_id) == failed_source["body"]["intent_id"]


@pytest.mark.parametrize("action", ["leave", "pause", "remove"])
async def test_cleanup_never_requests_content_deletion(
    database, failed_source, monkeypatch, action
):
    from app.adapters import qbittorrent

    calls = []

    class CleanupClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def find(self, **kwargs):
            return failed_source["client"].states

        async def cleanup_transfer(self, torrent_hash, *, remove):
            calls.append((torrent_hash, remove))
            return True

    monkeypatch.setattr(qbittorrent, "QbitClient", lambda *args: CleanupClient())
    async with database() as db, db.begin():
        row = await db.get(DownloadRecovery, failed_source["recovery_id"])
        row.evidence = {**row.evidence, "cleanup": action}
        from app.db.models import DownloadRecoverySettings

        settings = await db.get(DownloadRecoverySettings, 1)
        settings.configuration = {"sources": {"mam": {"cleanup": action}}}
    await download_recovery.cleanup(failed_source["recovery_id"])
    await download_recovery.cleanup(failed_source["recovery_id"])
    assert len(calls) == (0 if action == "leave" else 1)
    if calls:
        assert calls[0][1] == (action == "remove")
    async with database() as db:
        assert await db.scalar(select(DownloadIdentityClaim.active)) is True


async def test_queue_failure_rolls_back_block_and_retry_command(
    client, database, source, monkeypatch
):
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    operation = await start(client, source)
    await automatic_selection.run(UUID(operation["id"]))
    async with database() as db:
        selected_id = (await db.get(Operation, UUID(operation["id"]))).payload["selection_id"]
    response = await client.post(
        "/api/acquisition/downloads",
        json={"selection_id": selected_id},
        headers={"Idempotency-Key": "failed-transaction-key"},
    )
    identifier = UUID(response.json()["id"])

    async def crash(*args, **kwargs):
        raise RuntimeError("crash before commit")

    original = download_recovery.enqueue
    monkeypatch.setattr(download_recovery, "enqueue", crash)
    with pytest.raises(RuntimeError, match="crash"):
        async with database() as db, db.begin():
            attempt, _ = await download_attempts.locked(db, identifier)
            await download_recovery.failed(db, attempt, "Downloader reported failed")
    async with database() as db:
        assert not await db.scalar(select(ReleaseBlock.id))
        assert not await db.scalar(select(DownloadRecovery.id))
        assert (await db.get(AcquisitionSelection, UUID(selected_id))).state == "committed"
    monkeypatch.setattr(download_recovery, "enqueue", original)
    async with database() as db, db.begin():
        attempt, _ = await download_attempts.locked(db, identifier)
        await download_recovery.failed(db, attempt, "Downloader reported failed")
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadRecovery)) == 1


async def test_default_cap_stops_after_three_distinct_attempts(
    client, database, failed_source, monkeypatch
):
    current = failed_source["recovery_id"]
    for source_id, extension in [("502", "mp3"), ("503", "mp3")]:
        result_id, artifact_id, release = await additional_candidate(
            database, failed_source, source_id=source_id, format=extension
        )

        if source_id == "503":
            import base64

            from app.adapters.torrent_descriptor import inspect_torrent
            from app.db.models import SourceArtifact, SourceResult
            from app.security import encrypt_secrets
            from tests.torrent_fixture import torrent_bytes

            raw = torrent_bytes(name=b"Harbor", files=[{b"length": 13, b"path": [b"Harbor.mp3"]}])
            descriptor = await inspect_torrent(raw)
            release = release.model_copy(update={"size_bytes": 13})
            async with database() as db, db.begin():
                artifact = await db.get(SourceArtifact, artifact_id)
                artifact.descriptor = descriptor.model_dump(mode="json")
                artifact.sha256 = descriptor.artifact_sha256
                artifact.encrypted_content = encrypt_secrets(
                    {"torrent": base64.b64encode(raw).decode()}
                )
                artifact.release_snapshot = release.model_dump(mode="json")
                (await db.get(SourceResult, result_id)).release_snapshot = release.model_dump(
                    mode="json"
                )

        async def resolve(*args, artifact_id=artifact_id, release=release, **kwargs):
            return artifact_id, release

        monkeypatch.setattr(automatic_selection, "resolve_candidate", resolve)
        await download_recovery.run(current)
        async with database() as db:
            row = await db.get(DownloadRecovery, current)
            operation_id = row.replacement_id
            assert operation_id, row.message
        await automatic_selection.run(operation_id)
        await download_recovery.run(current)
        async with database() as db, db.begin():
            row = await db.get(DownloadRecovery, current)
            assert row.state == "retried", row.message
            attempt, _ = await download_attempts.locked(
                db, UUID(row.evidence["replacement_attempt_id"])
            )
            next_rows = await download_recovery.failed(db, attempt, "Downloader reported failed")
            current = next_rows[0].id
    await download_recovery.run(current)
    async with database() as db:
        row = await db.get(DownloadRecovery, current)
        assert row.state == "held" and "Gave up after 3" in row.message
        assert len(row.evidence["attempts"]) == 3
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 3
        assert await db.scalar(select(func.count()).select_from(ReleaseBlock)) == 3


async def test_notification_bridge_uses_transactional_deduplicated_events(
    database, failed_source, monkeypatch
):
    import sys
    from types import SimpleNamespace

    calls = []

    async def record_event(db, **values):
        calls.append(values)
        assert db.in_transaction()

    monkeypatch.setitem(
        sys.modules, "app.notifications.events", SimpleNamespace(record_event=record_event)
    )
    async with database() as db, db.begin():
        row = await db.get(DownloadRecovery, failed_source["recovery_id"])
        for name in ("stalled", "retried", "gave-up"):
            await download_recovery.event(db, row, name)
    assert [c["event_type"] for c in calls] == [
        "download.stalled",
        "download.retried",
        "download.gave_up",
    ]
    assert len({c["key"] for c in calls}) == 3
    assert all(c["key"].startswith(f"recovery:{failed_source['recovery_id']}:") for c in calls)
