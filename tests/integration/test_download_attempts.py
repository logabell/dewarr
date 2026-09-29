# ruff: noqa: F811
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.adapters.contracts import AdapterError, DownloadFile, FailureKind, SubmissionReceipt
from app.adapters.qbittorrent import QbitState
from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionReservation,
    AcquisitionSelection,
    DownloadAttempt,
    DownloadIdentityClaim,
    DownloadInspection,
    Integration,
    Operation,
    User,
    Work,
)
from app.domain import download_attempts as downloads
from app.domain.acquisition import reconcile_requests
from app.domain.work_merges import merge_works, preview_merge
from app.jobs.tasks import schedule_downloads
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import prepare, selection_route  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def selected(client, admin, selection_route, monkeypatch):
    selected = (await prepare(client, selection_route)).json()
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    return selected


async def start(client, selected, key="download-fixture-command"):
    return await client.post(
        "/api/acquisition/downloads",
        json={"selection_id": selected["id"]},
        headers={"Idempotency-Key": key},
    )


class Client:
    def __init__(self, database, descriptor):
        self.database, self.descriptor = database, descriptor
        self.calls, self.states = [], []
        self.fail = None
        self.before_find = None
        self.complete = False
        self.rename = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def capabilities(self):
        return None

    async def find(self, **kwargs):
        self.calls.append("find")
        if self.before_find:
            callback, self.before_find = self.before_find, None
            await callback()
        return self.states

    async def submit(self, content, *, attempt_tag, save_path, category):
        self.calls.append("submit")
        async with self.database() as db:
            attempt = await db.scalar(select(DownloadAttempt))
            assert attempt.state == "submitting" and attempt.external_may_exist
            assert attempt_tag == downloads.attempt_tag(attempt)
            assert (await db.get(AcquisitionSelection, attempt.selection_id)).state == "committed"
        self.states = [
            QbitState(
                external_id=self.descriptor["infohash_v1"],
                infohash_v1=self.descriptor["infohash_v1"],
                state="uploading" if self.complete else "downloading",
                completed=self.complete,
                save_path=save_path,
                files=[
                    DownloadFile(
                        relative_path=item["path"] + (".changed" if self.rename else ""),
                        size_bytes=item["size_bytes"],
                        complete=self.complete,
                    )
                    for item in self.descriptor["files"]
                ],
                tags={attempt_tag},
                category=category,
                auto_managed=False,
                progress=1 if self.complete else 0.25,
                total_bytes=self.descriptor["content_bytes"],
                all_files_selected=True,
            )
        ]
        if self.fail:
            raise self.fail
        return SubmissionReceipt()


@pytest.fixture
async def downloader(database, selected, monkeypatch):
    async with database() as db:
        row = await db.get(AcquisitionSelection, UUID(selected["id"]))
        result = Client(database, row.frozen["descriptor"])
    monkeypatch.setattr(downloads, "QbitClient", lambda *args: result)
    return result


async def row(database, identifier):
    async with database() as db:
        return await db.get(DownloadAttempt, UUID(identifier))


async def test_atomic_idempotent_start_and_private_projection(client, database, selected):
    replies = await asyncio.gather(*(start(client, selected) for _ in range(3)))
    assert all(r.status_code == 202 for r in replies), [r.text for r in replies]
    saved = replies[0].json()
    assert len({r.json()["id"] for r in replies}) == 1
    assert saved["state"] == "queued" and saved["can_cancel"]
    assert (await start(client, selected, "second-command-key")).json()["id"] == saved["id"]
    assert (await start(client, {"id": str(uuid4())}, "second-command-key")).status_code == 409
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
        assert await db.scalar(select(func.count()).select_from(DownloadIdentityClaim)) == 1
        attempt = await db.get(DownloadAttempt, UUID(saved["id"]))
        op = await db.get(Operation, attempt.operation_id)
        assert (
            await db.scalar(
                text("SELECT task_name FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": op.job_id},
            )
            == "acquisition.download"
        )
    assert (await client.delete("/api/acquisition/selections/" + selected["id"])).status_code == 409
    await reconcile_requests()
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        assert (await db.get(AcquisitionReservation, selection.reservation_id)).state == "committed"
    for forbidden in ("private", "/downloads", "torrent_hash", "receipt", "endpoint_key"):
        assert forbidden not in replies[0].text


async def test_rolled_back_start_leaves_no_attempt_claim_or_job(database, selected, admin):
    async with database() as db:
        actor = await db.get(User, UUID(admin["id"]))
        attempt = await downloads.start(db, actor, UUID(selected["id"]), "rollback-download-key")
        identifier, operation_id = attempt.id, attempt.operation_id
        await db.rollback()
    async with database() as db:
        assert await db.get(DownloadAttempt, identifier) is None
        assert await db.get(Operation, operation_id) is None
        assert await db.scalar(select(func.count()).select_from(DownloadIdentityClaim)) == 0
        assert (await db.get(AcquisitionSelection, UUID(selected["id"]))).state == "prepared"


async def test_submit_once_and_concurrent_redelivery_only_observes(
    client, database, selected, downloader
):
    saved = (await start(client, selected)).json()
    await asyncio.gather(*(downloads.run(UUID(saved["id"])) for _ in range(3)))
    await downloads.run(UUID(saved["id"]))
    assert downloader.calls.count("submit") == 1
    assert (await row(database, saved["id"])).state == "downloading"
    assert (await client.delete("/api/acquisition/downloads/" + saved["id"])).status_code == 409
    assert not (await client.get("/api/acquisition/downloads/" + saved["id"])).json()["can_cancel"]


async def test_lost_response_reconciles_without_second_add(client, database, selected, downloader):
    saved = (await start(client, selected)).json()
    downloader.fail = AdapterError(FailureKind.UNCERTAIN, "Submission response was lost")
    await downloads.run(UUID(saved["id"]))
    assert (await row(database, saved["id"])).state == "uncertain"
    await downloads.run(UUID(saved["id"]))
    assert (await row(database, saved["id"])).state == "downloading"
    assert downloader.calls.count("submit") == 1


async def test_missing_uncertain_transfer_never_resubmits(client, database, selected, downloader):
    saved = (await start(client, selected)).json()
    downloader.fail = TimeoutError()
    await downloads.run(UUID(saved["id"]))
    downloader.states = []
    for _ in range(3):
        await downloads.run(UUID(saved["id"]))
    assert (await row(database, saved["id"])).state == "uncertain"
    assert downloader.calls.count("submit") == 1


async def test_worker_crash_after_add_preserves_irreversible_boundary(
    client, database, selected, downloader
):
    class Crash(BaseException):
        pass

    saved = (await start(client, selected)).json()
    downloader.fail = Crash()
    with pytest.raises(Crash):
        await downloads.run(UUID(saved["id"]))
    crashed = await row(database, saved["id"])
    assert crashed.external_may_exist and crashed.state == "submitting"
    await downloads.run(crashed.id)
    assert downloader.calls.count("submit") == 1
    async with database() as db, db.begin():
        attempt = await db.get(DownloadAttempt, crashed.id)
        attempt.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await downloads.run(crashed.id)
    assert (await row(database, saved["id"])).state == "downloading"
    assert downloader.calls.count("submit") == 1


async def test_cancel_during_preflight_prevents_add_and_releases_claims(
    client, database, selected, downloader
):
    saved = (await start(client, selected)).json()

    async def cancel():
        assert (await client.delete("/api/acquisition/downloads/" + saved["id"])).status_code == 200

    downloader.before_find = cancel
    await downloads.run(UUID(saved["id"]))
    assert "submit" not in downloader.calls
    assert (await row(database, saved["id"])).state == "cancelled"
    assert (await start(client, selected)).json()["state"] == "cancelled"
    async with database() as db:
        assert not await db.scalar(select(DownloadIdentityClaim.active))


@pytest.mark.parametrize("change", ["settings", "permission", "reason", "recovery", "disable"])
async def test_recheck_authority_after_network_preflight(
    client, database, selected, downloader, change, monkeypatch
):
    saved = (await start(client, selected)).json()

    async def mutate():
        if change in {"recovery", "disable"}:
            monkeypatch.setattr(
                get_settings(),
                "recovery_mode" if change == "recovery" else "download_dispatch_enabled",
                change == "recovery",
            )
            return
        async with database() as db, db.begin():
            selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
            if change == "settings":
                (await db.get(Integration, selection.downloader_id)).credential_generation += 1
            elif change == "permission":
                (await db.get(User, selection.owner_id)).role = "viewer"
            else:
                reason = await db.scalar(
                    select(AcquisitionReason).where(
                        AcquisitionReason.intent_id == selection.intent_id
                    )
                )
                reason.active = False

    downloader.before_find = mutate
    await downloads.run(UUID(saved["id"]))
    attempt = await row(database, saved["id"])
    assert attempt.state == "held" and not attempt.external_may_exist
    assert "submit" not in downloader.calls


async def test_unrelated_existing_torrent_is_not_adopted(client, database, selected, downloader):
    saved = (await start(client, selected)).json()
    downloader.states = [
        QbitState(
            external_id=downloader.descriptor["infohash_v1"],
            infohash_v1=downloader.descriptor["infohash_v1"],
            state="downloading",
            completed=False,
            save_path="/downloads",
            files=[],
            tags=set(),
            category="book-search",
            auto_managed=False,
            progress=0.2,
            total_bytes=24,
            all_files_selected=True,
        )
    ]
    await downloads.run(UUID(saved["id"]))
    assert (await row(database, saved["id"])).state == "held"
    assert "submit" not in downloader.calls


async def test_completion_enqueues_review_not_ownership(client, database, selected, downloader):
    saved = (await start(client, selected)).json()
    downloader.complete = True
    await downloads.run(UUID(saved["id"]))
    attempt = await row(database, saved["id"])
    assert attempt.state == "complete" and attempt.inspection_id
    async with database() as db:
        inspection = await db.get(DownloadInspection, attempt.inspection_id)
        assert inspection.relative_path == downloader.descriptor["name"]
        assert inspection.state == "queued"
        assert (await db.get(Operation, inspection.operation_id)).job_id
    await downloads.run(attempt.id)
    assert downloader.calls.count("submit") == 1
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadInspection)) == 1
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        intent = await db.get(AcquisitionIntent, selection.intent_id)
    detail = (await client.get("/api/requests/" + str(intent.id))).json()
    assert detail["targets"][0]["state"] == "wanted"
    assert detail["targets"][0]["message"] == "Acquisition pending; check download activity"
    assert detail["targets"][0]["source_artifact_id"] == str(selection.artifact_id)


async def test_changed_completed_paths_are_held(client, database, selected, downloader):
    saved = (await start(client, selected)).json()
    downloader.complete = downloader.rename = True
    await downloads.run(UUID(saved["id"]))
    attempt = await row(database, saved["id"])
    assert attempt.state == "held" and attempt.inspection_id is None


async def test_duplicate_can_merge_into_a_book_with_an_outstanding_download(
    client, database, selected, admin
):
    assert (await start(client, selected)).status_code == 202
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        holder = UUID(selection.frozen["work_id"])
        duplicate = Work(
            title="Cloud Atlas (Unabridged)",
            authors=["David Mitchell"],
            provisional=True,
            catalog_public=False,
        )
        db.add(duplicate)
        await db.flush()
        duplicate_id = duplicate.id
        preview = await preview_merge(db, duplicate_id, holder, UUID(admin["id"]))
        await merge_works(db, UUID(admin["id"]), duplicate_id, holder, preview["revision"])
        await db.commit()
    async with database() as db:
        assert (await db.get(Work, duplicate_id)).redirect_to == holder
        assert await db.scalar(
            select(AcquisitionReservation).where(
                AcquisitionReservation.work_id == holder,
                AcquisitionReservation.state == "committed",
            )
        )
        assert (await db.get(AcquisitionSelection, UUID(selected["id"]))).state == "committed"


async def test_identity_merge_cannot_release_inflight_reservation(
    client, database, selected, admin
):
    await start(client, selected)
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        work = Work(title="Separate title", authors=["Another author"])
        db.add(work)
        await db.commit()
        source, target = UUID(selection.frozen["work_id"]), work.id
        preview = await preview_merge(db, source, target, UUID(admin["id"]))
        from fastapi import HTTPException

        with pytest.raises(HTTPException, match="Resolve outstanding"):
            await merge_works(db, UUID(admin["id"]), source, target, preview["revision"])
        await db.rollback()
    async with database() as db:
        assert (await db.get(Work, source)).redirect_to is None


async def test_due_monitor_scheduler_does_not_duplicate_live_job(client, database, selected):
    saved = (await start(client, selected)).json()
    attempt = await row(database, saved["id"])
    async with database() as db:
        original = (await db.get(Operation, attempt.operation_id)).job_id
    await schedule_downloads(0)
    async with database() as db, db.begin():
        assert (await db.get(Operation, attempt.operation_id)).job_id == original
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status='succeeded' WHERE id=:id"),
            {"id": original},
        )
    await schedule_downloads(0)
    async with database() as db:
        assert (await db.get(Operation, attempt.operation_id)).job_id != original


async def test_same_torrent_for_another_work_and_connection_alias_is_claimed_once(
    client, database, selected, selection_route, catalog
):
    from tests.integration.test_acquisition import body, request

    first = (await start(client, selected)).json()
    async with database() as db, db.begin():
        work = Work(title="Another book in the same pack", authors=["Fixture Writer"])
        db.add(work)
        original = await db.get(Integration, UUID(selection_route["downloader_id"]))
        alias = Integration(
            kind="qbittorrent",
            name="Same URL alias",
            credential_generation=1,
            base_url=original.base_url,
            encrypted_secrets=original.encrypted_secrets,
            config=original.config,
            status="connected",
        )
        db.add(alias)
        await db.flush()
        work_id, alias_id = work.id, alias.id
    intent = await request(
        client,
        body({**catalog, "work": work_id}, "audio", audio_library_id=str(catalog["library"])),
    )
    second = (
        await prepare(
            client,
            {
                **selection_route,
                "intent_id": intent["request"]["id"],
                "confirmed_work_id": str(work_id),
                "downloader_id": str(alias_id),
            },
            "second-pack-selection",
        )
    ).json()
    refused = await start(client, second, "second-pack-download")
    assert refused.status_code == 409, refused.text
    assert first["id"] not in refused.text
    assert (await client.delete("/api/acquisition/downloads/" + first["id"])).status_code == 200
    assert (await start(client, second, "second-pack-download")).status_code == 202


async def test_other_owner_cannot_read_cancel_or_recheck(client, database, selected):
    import httpx

    from app.security import hash_password

    saved = (await start(client, selected)).json()
    async with database() as db, db.begin():
        db.add(
            User(
                username="other-admin",
                display_name="Other",
                role="admin",
                password_hash=hash_password("another long password"),
            )
        )
    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as other:
        login = await other.post(
            "/api/auth/login", json={"username": "other-admin", "password": "another long password"}
        )
        other.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        path = "/api/acquisition/downloads/" + saved["id"]
        assert (await other.get(path)).status_code == 404
        assert (await other.delete(path)).status_code == 404
        assert (await other.post(path + "/recheck")).status_code == 404
        assert (await other.get("/api/acquisition/downloads")).json()["total"] == 0
        assert (await start(other, selected)).status_code == 404


async def test_dispatch_is_disabled_until_explicitly_activated(
    client, database, selected, monkeypatch
):
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", False)
    response = await start(client, selected)
    assert response.status_code == 409
    assert not (await client.get("/api/acquisition/selections/" + selected["id"])).json()[
        "dispatch_available"
    ]
    async with database() as db:
        assert not await db.scalar(select(DownloadAttempt.id))


async def test_downloader_change_after_submission_holds_identity_and_does_not_follow_new_endpoint(
    client, database, selected, downloader
):
    saved = (await start(client, selected)).json()
    downloader.fail = TimeoutError()
    await downloads.run(UUID(saved["id"]))
    before = list(downloader.calls)
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        connection = await db.get(Integration, selection.downloader_id)
        connection.base_url = "http://different-downloader.test"
        connection.credential_generation += 1
    await downloads.run(UUID(saved["id"]))
    assert downloader.calls == before
    attempt = await row(database, saved["id"])
    assert attempt.state == "held" and attempt.external_may_exist
    async with database() as db:
        assert await db.scalar(select(DownloadIdentityClaim.active))


async def test_dispatch_history_refuses_lossy_downgrade(client, database, selected):
    from tests.integration.test_correction_migration import legacy_request_policy_fixture, migrate

    await start(client, selected)
    async with database() as db:
        before = await db.scalar(text("SELECT version_num FROM alembic_version"))
    await legacy_request_policy_fixture(database)
    result = await migrate("downgrade", "0017_selections")
    assert result.returncode != 0
    assert "Capacity history requires" in result.stderr
    async with database() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == before


async def test_known_padding_is_not_mistaken_for_missing_payload(
    client, database, selected, downloader
):
    import base64

    from app.adapters.torrent_descriptor import inspect_torrent
    from app.db.models import SourceArtifact
    from app.security import encrypt_secrets
    from tests.torrent_fixture import torrent_bytes

    raw = torrent_bytes(
        files=[
            {b"length": 12, b"path": [b"01 - Harbor.m4b"]},
            {b"length": 12, b"path": [b"padding"], b"attr": b"p"},
            {b"length": 12, b"path": [b"02 - Roads.m4b"]},
        ]
    )
    descriptor = (await inspect_torrent(raw)).model_dump(mode="json")
    assert descriptor["padding_bytes"] == 12 and descriptor["content_bytes"] == 24
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        artifact = await db.get(SourceArtifact, selection.artifact_id)
        artifact.descriptor, artifact.sha256 = descriptor, descriptor["artifact_sha256"]
        artifact.encrypted_content = encrypt_secrets({"torrent": base64.b64encode(raw).decode()})
        selection.frozen = {
            **selection.frozen,
            "descriptor": descriptor,
            "artifact_sha256": descriptor["artifact_sha256"],
        }
    downloader.descriptor = descriptor
    saved = (await start(client, selected)).json()
    await downloads.run(UUID(saved["id"]))
    downloader.states[0] = downloader.states[0].model_copy(
        update={
            "reported_complete": True,
            "completed": False,
            "total_bytes": descriptor["torrent_bytes"],
            "progress": 1,
            "state": "uploading",
            "files": [
                item.model_copy(update={"complete": True}) for item in downloader.states[0].files
            ],
        }
    )
    await downloads.run(UUID(saved["id"]))
    assert (await row(database, saved["id"])).inspection_id
    assert downloader.calls.count("submit") == 1


async def test_single_file_dispatch_hands_off_exact_file_path(
    client, database, selected, downloader
):
    import base64
    import hashlib

    import libtorrent as lt

    from app.adapters.torrent_descriptor import inspect_torrent
    from app.db.models import SourceArtifact
    from app.security import encrypt_secrets

    raw = lt.bencode(
        {
            b"info": {
                b"name": b"Harbor.m4b",
                b"length": 12,
                b"piece length": 16384,
                b"pieces": hashlib.sha1(b"fixture data").digest(),
            }
        }
    )
    descriptor = (await inspect_torrent(raw)).model_dump(mode="json")
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        artifact = await db.get(SourceArtifact, selection.artifact_id)
        artifact.descriptor, artifact.sha256 = descriptor, descriptor["artifact_sha256"]
        artifact.encrypted_content = encrypt_secrets({"torrent": base64.b64encode(raw).decode()})
        selection.frozen = {
            **selection.frozen,
            "descriptor": descriptor,
            "artifact_sha256": descriptor["artifact_sha256"],
        }
    downloader.descriptor, downloader.complete = descriptor, True
    response = await start(client, selected)
    assert response.status_code == 202, response.text
    await downloads.run(UUID(response.json()["id"]))
    attempt = await row(database, response.json()["id"])
    assert attempt.state == "complete" and attempt.inspection_id
    async with database() as db:
        inspection = await db.get(DownloadInspection, attempt.inspection_id)
        assert inspection.relative_path == "Harbor.m4b"


async def test_removed_attempt_tag_cannot_confirm_or_restart_transfer(
    client, database, selected, downloader
):
    saved = (await start(client, selected)).json()
    await downloads.run(UUID(saved["id"]))
    downloader.states[0].tags = set()
    await downloads.run(UUID(saved["id"]))
    attempt = await row(database, saved["id"])
    assert attempt.state == "uncertain" and attempt.external_may_exist
    assert not attempt.inspection_id
    assert downloader.calls.count("submit") == 1


async def test_download_history_is_filtered_by_book_and_account(client, selected, database):
    from tests.integration.test_discovery import login_member

    response = await start(client, selected)
    assert response.status_code == 202, response.text
    work_id = selected["work_id"]
    history = await client.get("/api/acquisition/downloads", params={"work_id": work_id})
    assert history.status_code == 200
    assert history.json()["total"] == 1
    async with database() as db:
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        assert history.json()["items"][0]["source"] == selection.frozen["release"]["source"]
    unrelated = await client.get("/api/acquisition/downloads", params={"work_id": str(uuid4())})
    assert unrelated.json()["total"] == 0
    await login_member(client)
    assert (await client.get("/api/acquisition/downloads", params={"work_id": work_id})).json()[
        "total"
    ] == 0


@pytest.mark.parametrize("change", ["withdraw", "disable-dispatch"])
async def test_selected_transfer_rechecks_authority_before_start(
    client, database, selected, monkeypatch, change
):
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        descriptor = selection.frozen["descriptor"]
        paths = [f["path"] for f in descriptor["files"]]
        selection.frozen = {**selection.frozen, "selected_paths": paths}
        intent_id = selection.intent_id

    class StoppedClient(Client):
        async def submit(self, content, *, stopped=False, **kwargs):
            assert stopped
            receipt = await super().submit(content, **kwargs)
            self.states[0].state = "stoppedDL"
            for i, file in enumerate(self.states[0].files):
                file.index, file.priority = i, 1
            return receipt

        async def select_files(self, state, expected, paths):
            assert state.state == "stoppedDL"
            if change == "withdraw":
                async with database() as db, db.begin():
                    for reason in await db.scalars(
                        select(AcquisitionReason).where(AcquisitionReason.intent_id == intent_id)
                    ):
                        reason.active = False
            else:
                monkeypatch.setattr(get_settings(), "download_dispatch_enabled", False)

        async def start_transfer(self, key):
            self.calls.append("started-after-revocation")
            self.states[0].state = "downloading"

    downloader = StoppedClient(database, descriptor)
    monkeypatch.setattr(downloads, "QbitClient", lambda *args: downloader)
    response = await start(client, selected)
    assert response.status_code == 202, response.text
    await downloads.run(UUID(response.json()["id"]))
    assert "started-after-revocation" not in downloader.calls
    assert downloader.states[0].state == "stoppedDL"
    saved = await row(database, response.json()["id"])
    assert saved.state == "held"
    assert not (saved.receipt or {}).get("file_selection_start_attempted")


@pytest.mark.parametrize("outcome", ["started", "never-started", "paused-later"])
async def test_collection_start_acknowledgement_keeps_polling_without_restarting(
    client, database, selected, monkeypatch, outcome
):
    async with database() as db, db.begin():
        selection = await db.get(AcquisitionSelection, UUID(selected["id"]))
        descriptor = selection.frozen["descriptor"]
        selection.frozen = {
            **selection.frozen,
            "selected_paths": [f["path"] for f in descriptor["files"]],
        }

    class DelayedStartClient(Client):
        async def submit(self, content, *, stopped=False, **kwargs):
            assert stopped
            receipt = await super().submit(content, **kwargs)
            self.states[0].state = "stoppedDL"
            for i, file in enumerate(self.states[0].files):
                file.index, file.priority = i, 1
            return receipt

        async def select_files(self, state, expected, paths):
            self.calls.append("select-files")

        async def start_transfer(self, key):
            self.calls.append("start-transfer")
            # Successful API acknowledgement; the status snapshot still lags.

    downloader = DelayedStartClient(database, descriptor)
    monkeypatch.setattr(downloads, "QbitClient", lambda *args: downloader)
    response = await start(client, selected)
    identifier = UUID(response.json()["id"])
    await downloads.run(identifier)
    saved = await row(database, str(identifier))
    assert saved.state == "downloading" and saved.next_check_at is not None
    assert not saved.receipt.get("file_selection_start_observed")
    if outcome == "never-started":
        async with database() as db, db.begin():
            saved = await db.get(DownloadAttempt, identifier)
            saved.receipt = {
                **saved.receipt,
                "file_selection_start_requested_at": (
                    datetime.now(UTC) - timedelta(minutes=3)
                ).isoformat(),
            }
    else:
        downloader.states[0].state = "downloading"
    await downloads.run(identifier)
    saved = await row(database, str(identifier))
    assert saved.state == ("held" if outcome == "never-started" else "downloading")
    if outcome == "paused-later":
        downloader.states[0].state = "stoppedDL"
        await downloads.run(identifier)
        assert (await row(database, str(identifier))).state == "held"
    assert downloader.calls.count("submit") == 1
    assert downloader.calls.count("select-files") == 1
    assert downloader.calls.count("start-transfer") == 1
