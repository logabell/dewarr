# ruff: noqa: F811
"""Unblocking a release continues the interrupted transfer instead of starting over."""

from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update

from app.db.models import (
    AcquisitionReason,
    AcquisitionReservation,
    AcquisitionSelection,
    AuditEvent,
    DownloadAttempt,
    DownloadIdentityClaim,
    DownloadInspection,
    DownloadRecovery,
    ReleaseBlock,
    User,
)
from app.domain import book_sources, download_attempts, download_recovery, release_blocklist
from app.main import create_app
from app.security import hash_password
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_automatic_selection import (  # noqa: F401
    additional_candidate,
    source,
    start,
)
from tests.integration.test_book_sources import begin, read
from tests.integration.test_failed_download_recovery import failed_source  # noqa: F401
from tests.integration.test_mam_sources import configure as configure_mam  # noqa: F401
from tests.integration.test_mam_sources import source_http  # noqa: F401
from tests.mam_fixture import release_row, search_response

pytestmark = pytest.mark.integration

BLOCKLIST = "/api/acquisition/recovery/blocklist"


async def given_up(database, recovery_id, *, evidence=None):
    """The state a recovery reaches when no replacement qualifies."""
    async with database() as db, db.begin():
        row = await db.get(DownloadRecovery, recovery_id)
        if evidence:
            row.evidence = {**row.evidence, **evidence}
        await download_recovery.hold(db, row, "No release qualified for automatic download")


async def only_block(client):
    blocks = (await client.get(BLOCKLIST)).json()
    assert len(blocks) == 1
    return blocks[0]["id"]


async def counts(database):
    async with database() as db:
        return {
            "recoveries": await db.scalar(select(func.count()).select_from(DownloadRecovery)),
            "claims": await db.scalar(
                select(func.count())
                .select_from(DownloadIdentityClaim)
                .where(DownloadIdentityClaim.active.is_(True))
            ),
            "attempts": await db.scalar(select(func.count()).select_from(DownloadAttempt)),
        }


async def unchanged(database, failed_source):
    async with database() as db:
        attempt = await db.get(DownloadAttempt, failed_source["attempt_id"])
        selection = await db.get(AcquisitionSelection, failed_source["selection_id"])
        reservation = await db.get(AcquisitionReservation, selection.reservation_id)
        return attempt.state, selection.state, reservation.state


async def test_resume_continues_the_same_transfer_through_a_wrapped_layout(
    client, database, failed_source
):
    await given_up(database, failed_source["recovery_id"])
    block_id = await only_block(client)
    response = await client.post(f"{BLOCKLIST}/{block_id}/resume")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unblocked"] and body["resumed"] == 1
    assert body["attempts"][0]["attempt_id"] == str(failed_source["attempt_id"])
    async with database() as db:
        assert not (await db.get(ReleaseBlock, UUID(block_id))).active
        assert await db.get(DownloadRecovery, failed_source["recovery_id"]) is None
        selection = await db.get(AcquisitionSelection, failed_source["selection_id"])
        assert selection.state == "committed"
        assert (await db.get(AcquisitionReservation, selection.reservation_id)).state == "committed"
        attempt = await db.get(DownloadAttempt, failed_source["attempt_id"])
        assert attempt.recovery_observation is None and attempt.external_may_exist
        assert "Resumed" in attempt.message
        audit = await db.scalar(
            select(AuditEvent).where(AuditEvent.action == "acquisition.download.resumed")
        )
        assert audit and audit.detail["block_id"] == block_id
    # The downloader now shows a finished transfer inside a client-added wrapper folder.
    mock = failed_source["client"]
    mock.states = [
        state.model_copy(
            update={
                "state": "uploading",
                "completed": True,
                "progress": 1,
                "seeders": 9,
                "files": [
                    file.model_copy(
                        update={"relative_path": "Harbor/" + file.relative_path, "complete": True}
                    )
                    for file in state.files
                ],
            }
        )
        for state in mock.states
    ]
    await download_attempts.run(failed_source["attempt_id"])
    async with database() as db:
        attempt = await db.get(DownloadAttempt, failed_source["attempt_id"])
        assert attempt.state == "complete", attempt.message
        inspection = await db.get(DownloadInspection, attempt.inspection_id)
        # The torrent has its own root folder "Harbor"; the client put one more around it.
        assert inspection.relative_path == "Harbor/Harbor"
    # Nothing was added to the downloader and the original claim still owns the hash.
    assert mock.calls.count("submit") == 1
    assert (await counts(database)) == {"recoveries": 0, "claims": 1, "attempts": 1}


async def test_second_resume_is_a_harmless_repeat(client, database, failed_source):
    await given_up(database, failed_source["recovery_id"])
    block_id = await only_block(client)
    assert (await client.post(f"{BLOCKLIST}/{block_id}/resume")).json()["resumed"] == 1
    again = await client.post(f"{BLOCKLIST}/{block_id}/resume")
    assert again.status_code == 200
    assert again.json() == {
        "block_id": block_id,
        "unblocked": True,
        "resumed": 0,
        "attempts": [],
    }
    assert (await unchanged(database, failed_source))[1:] == ("committed", "committed")


async def test_plain_removal_leaves_the_failed_transfer_alone(client, database, failed_source):
    await given_up(database, failed_source["recovery_id"])
    block_id = await only_block(client)
    assert (await client.delete(f"{BLOCKLIST}/{block_id}")).status_code == 204
    async with database() as db:
        assert not (await db.get(ReleaseBlock, UUID(block_id))).active
        assert await db.get(DownloadRecovery, failed_source["recovery_id"])
    assert await unchanged(database, failed_source) == ("held", "cancelled", "released")


@pytest.mark.parametrize("state", ["queued", "searching", "selecting", "retried", "complete"])
async def test_a_failure_that_already_triggered_a_replacement_is_not_resumed(
    client, database, failed_source, state
):
    async with database() as db, db.begin():
        (await db.get(DownloadRecovery, failed_source["recovery_id"])).state = state
    block_id = await only_block(client)
    response = await client.post(f"{BLOCKLIST}/{block_id}/resume")
    assert response.status_code == 200
    # Unblocking still happens; a recovery that is not "given up" is not even a candidate,
    # so the transfer and its recovery are left exactly as they were.
    assert response.json() == {
        "block_id": block_id,
        "unblocked": True,
        "resumed": 0,
        "attempts": [],
    }
    async with database() as db:
        assert not (await db.get(ReleaseBlock, UUID(block_id))).active
        assert (await db.get(DownloadRecovery, failed_source["recovery_id"])).state == state
    assert await unchanged(database, failed_source) == ("held", "cancelled", "released")


async def test_a_problem_reported_by_a_person_is_never_resumed(client, database, failed_source):
    await given_up(database, failed_source["recovery_id"], evidence={"reported": True})
    block_id = await only_block(client)
    body = (await client.post(f"{BLOCKLIST}/{block_id}/resume")).json()
    assert body["resumed"] == 0 and "reported" in body["attempts"][0]["message"]
    assert await unchanged(database, failed_source) == ("held", "cancelled", "released")


async def test_another_block_for_the_same_release_keeps_it_blocked(client, database, failed_source):
    await given_up(database, failed_source["recovery_id"])
    block_id = await only_block(client)
    async with database() as db, db.begin():
        original = await db.get(ReleaseBlock, UUID(block_id))
        db.add(
            ReleaseBlock(
                work_id=original.work_id,
                medium=original.medium,
                release_key="second-entry",
                source=original.source,
                title=original.title,
                identities=original.identities,
                reason="Reported as wrong",
                actor_id=original.actor_id,
                automatic=False,
            )
        )
    body = (await client.post(f"{BLOCKLIST}/{block_id}/resume")).json()
    assert body["resumed"] == 0 and "Another blocklist entry" in body["attempts"][0]["message"]
    async with database() as db:
        assert not (await db.get(ReleaseBlock, UUID(block_id))).active
    assert await unchanged(database, failed_source) == ("held", "cancelled", "released")


async def test_a_withdrawn_request_rolls_the_whole_resume_back(client, database, failed_source):
    await given_up(database, failed_source["recovery_id"])
    block_id = await only_block(client)
    async with database() as db, db.begin():
        await db.execute(update(AcquisitionReason).values(active=False))
    body = (await client.post(f"{BLOCKLIST}/{block_id}/resume")).json()
    assert body["resumed"] == 0
    assert "paused, withdrawn or already satisfied" in body["attempts"][0]["message"]
    async with database() as db:
        assert await db.get(DownloadRecovery, failed_source["recovery_id"])
        assert not (await db.get(ReleaseBlock, UUID(block_id))).active
    assert await unchanged(database, failed_source) == ("held", "cancelled", "released")


async def test_a_recovered_attempt_that_is_no_longer_held_is_not_touched(
    client, database, failed_source
):
    await given_up(database, failed_source["recovery_id"])
    block_id = await only_block(client)
    async with database() as db, db.begin():
        (await db.get(DownloadAttempt, failed_source["attempt_id"])).state = "downloading"
    body = (await client.post(f"{BLOCKLIST}/{block_id}/resume")).json()
    # Not even a candidate: the screen only considers held attempts.
    assert body["unblocked"] and body["resumed"] == 0 and body["attempts"] == []
    async with database() as db:
        assert await db.get(DownloadRecovery, failed_source["recovery_id"])


async def test_unknown_block_is_a_404(client, admin):
    assert (await client.post(f"{BLOCKLIST}/{UUID(int=7)}/resume")).status_code == 404
    assert (await client.delete(f"{BLOCKLIST}/{UUID(int=7)}")).status_code == 404


async def test_only_administrators_can_unblock_or_resume(client, database, failed_source):
    await given_up(database, failed_source["recovery_id"])
    block_id = await only_block(client)
    async with database() as db, db.begin():
        db.add(
            User(
                username="reader",
                display_name="Reader",
                role="member",
                password_hash=hash_password("private member password"),
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as member:
        login = await member.post(
            "/api/auth/login", json={"username": "reader", "password": "private member password"}
        )
        member.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (await member.post(f"{BLOCKLIST}/{block_id}/resume")).status_code == 403
        assert (await member.delete(f"{BLOCKLIST}/{block_id}")).status_code == 403
    async with database() as db:
        assert (await db.get(ReleaseBlock, UUID(block_id))).active
    assert await unchanged(database, failed_source) == ("held", "cancelled", "released")


async def test_resumable_screen_matches_only_the_blocked_release(database, client, failed_source):
    await given_up(database, failed_source["recovery_id"])
    async with database() as db:
        block = (await db.scalars(select(ReleaseBlock))).one()
        assert await release_blocklist.blocked(
            db,
            block.work_id,
            block.medium,
            {"source": "mam", "source_id": "501"},
        )
        from app.domain import download_resume

        assert await download_resume.resumable_attempt_ids(db, block) == [
            failed_source["attempt_id"]
        ]
        other = ReleaseBlock(
            work_id=block.work_id,
            medium=block.medium,
            release_key="unrelated",
            source="mam",
            title="Other",
            identities=["release:unrelated"],
            reason="Reported",
            actor_id=block.actor_id,
            automatic=False,
        )
        assert await download_resume.resumable_attempt_ids(db, other) == []
        block.medium = "ebook"
        assert await download_resume.resumable_attempt_ids(db, block) == []


async def test_sources_page_lists_the_block_on_exactly_the_blocked_release(
    client, admin, database, catalog, source_http
):
    await configure_mam(client)
    source_http["body"] = search_response(
        data=[
            release_row(id=1, title="Harbor", author_info='{"1":"Writer"}'),
            release_row(id=2, title="Harbor", author_info='{"1":"Writer"}'),
        ],
        found=2,
        total=2,
    )
    saved = await begin(client, catalog)
    await book_sources.run(UUID(saved["id"]), "mam")
    items = {
        row["release"]["source_id"]: row
        for row in (await read(client, saved["id"])).json()["items"]
    }
    assert all(row["block"] is None for row in items.values())
    release = items["1"]["release"]
    _, keys = release_blocklist.release_keys(release)
    async with database() as db, db.begin():
        db.add(
            ReleaseBlock(
                work_id=UUID(str(catalog["work"])),
                medium=release["medium"],
                release_key="one",
                source="mam",
                title=release["title"],
                identities=keys,
                reason="Completed files differ from the inspected torrent",
                actor_id=UUID(admin["id"]),
                automatic=True,
            )
        )
    items = {
        row["release"]["source_id"]: row
        for row in (await read(client, saved["id"])).json()["items"]
    }
    block = items["1"]["block"]
    assert block["reason"] == "Completed files differ from the inspected torrent"
    assert block["automatic"] is True and block["can_resume"] is False and block["id"]
    assert items["2"]["block"] is None
    # A removed block disappears from the page; a block for another format never shows.
    async with database() as db, db.begin():
        row = await db.scalar(select(ReleaseBlock))
        row.medium = "ebook" if release["medium"] == "audio" else "audio"
    items = {
        row["release"]["source_id"]: row
        for row in (await read(client, saved["id"])).json()["items"]
    }
    assert items["1"]["block"] is None
    async with database() as db, db.begin():
        row = await db.scalar(select(ReleaseBlock))
        row.medium, row.active = release["medium"], False
    items = {
        row["release"]["source_id"]: row
        for row in (await read(client, saved["id"])).json()["items"]
    }
    assert items["1"]["block"] is None
