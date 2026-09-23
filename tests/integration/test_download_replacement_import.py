# ruff: noqa: F811
"""A reported owned book is replaced through real inspection/publication, preserving its copy."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import libtorrent as lt
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.contracts import DownloadFile, SubmissionReceipt
from app.adapters.mam import MAMRelease
from app.adapters.qbittorrent import QbitState
from app.adapters.torrent_descriptor import inspect_torrent
from app.db.models import (
    AcquisitionSelection,
    DownloadAttempt,
    DownloadRecovery,
    ImportEntry,
    SourceArtifact,
    SourceResult,
)
from app.domain import automatic_selection, download_attempts, download_recovery
from app.domain.download_reviews import validate_inspection
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.integration.test_automatic_acquisition import (
    test_search_to_automatic_download_and_confirmed_member_library as original_import,
)
from tests.integration.test_download_reviews import review_account  # noqa: F401
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_execution import ready_route  # noqa: F401
from tests.media_fixtures import epub

pytestmark = pytest.mark.integration


async def test_reported_owned_copy_gets_a_new_confirmed_import_without_deleting_library_files(
    client, admin, database, ready_route, review_account, monkeypatch
):
    await original_import(
        client,
        admin,
        database,
        ready_route,
        review_account,
        monkeypatch,
        "ebook",
        False,
        request_limits=False,
    )
    old_files = {
        p: (p.read_bytes(), p.stat().st_ino) for p in ready_route["target"].rglob("*.epub")
    }
    async with database() as db:
        original = await db.scalar(select(AcquisitionSelection))
        old_entry = await db.scalar(select(ImportEntry))
        old_attempt = await db.scalar(select(DownloadAttempt))
        old_artifact = await db.get(SourceArtifact, original.artifact_id)
        search_id = UUID(original.frozen["automatic_selection"]["search_id"])
    reply = await client.post(
        "/api/acquisition/recovery/reports",
        json={
            "selection_id": str(original.id),
            "asset_id": str(old_entry.asset_id),
            "reason": "wrong-book",
        },
    )
    assert reply.status_code == 202, reply.text
    recovery_id = UUID(reply.json()[0]["id"])
    async with database() as db:
        with pytest.raises(HTTPException, match="release was rejected"):
            await validate_inspection(db, old_attempt.inspection_id)
    replacement_file = ready_route["source"] / "replacement.epub"
    epub(replacement_file, isbn="9781234567897")
    content = replacement_file.read_bytes()
    raw = lt.bencode(
        {
            b"info": {
                b"name": replacement_file.name.encode(),
                b"length": len(content),
                b"piece length": 16384,
                b"pieces": b"".join(
                    hashlib.sha1(content[pos : pos + 16384]).digest()
                    for pos in range(0, len(content), 16384)
                ),
            }
        }
    )
    descriptor = await inspect_torrent(raw)
    release = MAMRelease.model_validate(
        {
            **old_artifact.release_snapshot,
            "source_id": "503",
            "size_bytes": len(content),
            "observed_at": datetime.now(UTC).isoformat(),
        }
    )
    async with database() as db, db.begin():
        artifact = SourceArtifact(
            owner_id=original.owner_id,
            source_key="mam",
            source_id="503",
            source_generation=1,
            sha256=descriptor.artifact_sha256,
            descriptor=descriptor.model_dump(mode="json"),
            release_snapshot=release.model_dump(mode="json"),
            encrypted_content=encrypt_secrets({"torrent": base64.b64encode(raw).decode()}),
        )
        db.add(artifact)
        db.add(
            SourceResult(
                owner_id=original.owner_id,
                operation_id=search_id,
                source_key="mam",
                source_generation=1,
                expires_at=datetime.now(UTC) + timedelta(minutes=25),
                encrypted_reference=encrypt_secrets({"link": None}),
                release_snapshot=release.model_dump(mode="json"),
            )
        )
        await db.flush()
        artifact_id = artifact.id

    async def resolve(*args, **kwargs):
        return artifact_id, release

    monkeypatch.setattr(automatic_selection, "resolve_candidate", resolve)

    class ReplacementClient:
        def __init__(self):
            self.states, self.submits = [], 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def capabilities(self):
            pass

        async def find(self, **kwargs):
            return self.states

        async def submit(self, content, *, attempt_tag, save_path, category):
            self.submits += 1
            self.states = [
                QbitState(
                    external_id=descriptor.infohash_v1,
                    infohash_v1=descriptor.infohash_v1,
                    state="uploading",
                    completed=True,
                    save_path=save_path,
                    tags={attempt_tag},
                    category=category,
                    auto_managed=False,
                    progress=1,
                    total_bytes=descriptor.torrent_bytes,
                    all_files_selected=True,
                    files=[
                        DownloadFile(relative_path=f.path, size_bytes=f.size_bytes, complete=True)
                        for f in descriptor.files
                    ],
                )
            ]
            return SubmissionReceipt()

    replacement = ReplacementClient()
    monkeypatch.setattr(download_attempts, "QbitClient", lambda *args: replacement)
    await download_recovery.run(recovery_id)
    async with database() as db:
        row = await db.get(DownloadRecovery, recovery_id)
        assert row.state == "selecting", row.message
        replacement_id = row.replacement_id
    await automatic_selection.run(replacement_id)
    await download_recovery.run(recovery_id)
    await get_queue().run_worker_async(
        wait=False, concurrency=1, listen_notify=False, install_signal_handlers=False
    )
    async with database() as db:
        row = await db.get(DownloadRecovery, recovery_id)
        assert row.state == "retried", row.message
        entries = list(await db.scalars(select(ImportEntry).order_by(ImportEntry.created_at)))
        assert len(entries) == 2, [(e.state, e.message) for e in entries]
        assert all(e.state == "confirmed" for e in entries), [(e.state, e.message) for e in entries]
        assert entries[1].asset_id != old_entry.asset_id
        assert (await db.get(DownloadAttempt, old_attempt.id)).state == "held"
    assert replacement.submits == 1
    assert len(list(ready_route["target"].rglob("*.epub"))) == 2
    for path, (data, inode) in old_files.items():
        assert path.read_bytes() == data and path.stat().st_ino == inode


async def test_reported_imported_pack_reopens_every_members_request(
    client, admin, database, ready_route, review_account, monkeypatch
):
    from app.db.models import ReportedDownloadAsset, Version

    await original_import(
        client,
        admin,
        database,
        ready_route,
        review_account,
        monkeypatch,
        "ebook",
        False,
        request_limits=True,
        series_pack=True,
        automatic_group=True,
    )
    async with database() as db:
        members = list(await db.scalars(select(AcquisitionSelection)))
        assert len(members) == 2
        asset_id = await db.scalar(
            select(ImportEntry.asset_id)
            .join(Version)
            .where(Version.work_id == UUID(members[0].frozen["origin_work_id"]))
        )
    reply = await client.post(
        "/api/acquisition/recovery/reports",
        json={
            "selection_id": str(members[0].id),
            "asset_id": str(asset_id),
            "reason": "wrong-book",
        },
    )
    assert reply.status_code == 202, reply.text
    async with database() as db:
        exclusions = list(await db.scalars(select(ReportedDownloadAsset)))
        assert len(exclusions) == 2
        recoveries = list(await db.scalars(select(DownloadRecovery)))
        assert len(recoveries) == 2
        attempt = await db.scalar(select(DownloadAttempt))
    for member in members:
        detail = await client.get(
            f"/api/acquisition/downloads/{attempt.id}",
            params={"work_id": member.frozen["origin_work_id"]},
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["selection_id"] == str(member.id)
        assert detail.json()["attempt_chain"][0]["selection_id"] == str(member.id)
    for recovery in recoveries:
        await download_recovery.run(recovery.id)
    async with database() as db:
        for recovery in await db.scalars(select(DownloadRecovery)):
            assert recovery.state in {"searching", "selecting"}, recovery.message
        assert set(await db.scalars(select(ImportEntry.state))) == {"confirmed"}
    assert len(list(ready_route["target"].rglob("*.epub"))) == 2


async def test_wrong_book_inspection_creates_blocklist_and_replacement_command(
    client, admin, database, ready_route, review_account, monkeypatch
):
    from app.db.models import ReleaseBlock

    await original_import(
        client,
        admin,
        database,
        ready_route,
        review_account,
        monkeypatch,
        "ebook",
        False,
        request_limits=True,
        series_pack=True,
        counterfeit=True,
    )
    async with database() as db:
        block = await db.scalar(select(ReleaseBlock))
        recovery = await db.scalar(select(DownloadRecovery))
        assert block and block.active
        assert recovery and recovery.search_id
        assert recovery.reason.startswith("Inspected release rejected")
        assert not await db.scalar(select(ImportEntry.id))
