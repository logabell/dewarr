# ruff: noqa: F811
"""Reviewed pack -> one transfer -> independent real-file imports and ABS receipts."""

import base64
import hashlib
from datetime import UTC, datetime
from uuid import UUID

import libtorrent as lt
import pytest
from sqlalchemy import func, select

from app.adapters.mam import MAMRelease
from app.adapters.torrent_descriptor import inspect_torrent
from app.config import get_settings
from app.db.models import (
    AcquisitionSelection,
    AutomaticImport,
    DownloadAttempt,
    DownloadFulfillment,
    ImportEntry,
    Integration,
    SourceArtifact,
    SourceConnection,
    Version,
)
from app.domain import download_attempts as downloads
from app.importing import execution
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.integration.test_acquisition import body, request
from tests.integration.test_acquisition_selections import prepare
from tests.integration.test_download_attempts import Client
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_execution import ready_route  # noqa: F401
from tests.integration.test_inspection_matching import edition
from tests.integration.test_shared_downloads import grouped
from tests.media_fixtures import epub

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "already_owned,delayed_scan,ambiguous_first",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (False, False, True),
        (False, True, True),
    ],
)
async def test_reviewed_pack_imports_books_independently_and_preserves_seeded_files(
    client,
    admin,
    database,
    ready_route,
    monkeypatch,
    already_owned,
    delayed_scan,
    ambiguous_first,
    manual_prepare=False,
    selected_files=False,
    reviewed_tags=False,
    swapped_review=False,
):
    route = ready_route
    # Exercise the finite acquisition/import graph, not wall-clock cron ticks.
    # Periodic deferral can enqueue unrelated work while a wait=False worker is
    # shutting down; scheduler and long-running-worker behavior have separate tests.
    monkeypatch.setattr(get_queue().periodic_registry, "periodic_tasks", {})
    first_id = UUID(route["plan"]["document"]["groups"][0]["work_id"])
    first = await edition(database, work_id=first_id)
    second = await edition(
        database, title="Second Harbor", identifiers={"isbn_13": "9780140328721"}
    )
    pack = route["source"] / "pack"
    if manual_prepare:
        # Replace the setup probe's generic filename with the catalog title.
        # Leaving both creates an unidentifiable extra primary file in the torrent.
        (pack / "book.epub").rename(pack / "First Harbor.epub")
    epub(
        pack / ("First Harbor.epub" if manual_prepare else "book.epub"),
        isbn=None if reviewed_tags else "9781234567897",
    )
    epub(
        pack / ("Second Harbor.epub" if manual_prepare else "second.epub"),
        title="Second Harbor",
        isbn=None if reviewed_tags else "9780140328721",
    )
    if ambiguous_first:
        epub(pack / "alternative.epub", isbn="9781234567897")
    # This extra file is not covered by either reviewed book request.
    epub(
        pack / ("Unrequested extra.epub" if manual_prepare else "unrequested.epub"),
        title="Unrequested extra",
    )
    if manual_prepare:
        from tests.pack_fixture import catalog as pack_catalog

        third = await edition(
            database, title="Unrequested extra", identifiers={"isbn_13": "9780000000002"}
        )
        await pack_catalog(
            database,
            admin["id"],
            [first["work"], second["work"], third["work"]],
            name="Harbor collection",
        )
        reviewed = await client.post(
            "/api/catalog/series/hardcover/pack-series/main-books",
            headers={"Idempotency-Key": "manual-import-main-books"},
            json={
                "work_ids": [str(first["work"]), str(second["work"])],
                "expected_generation": 1,
                "expected_review_id": None,
                "confirm_main_membership": True,
            },
        )
        assert reviewed.status_code == 201, reviewed.text
    originals = {path.name: path.read_bytes() for path in sorted(pack.glob("*.epub"))}
    payload = b"".join(originals.values())
    raw = lt.bencode(
        {
            b"info": {
                b"name": b"pack",
                b"piece length": 16384,
                b"files": [
                    {b"length": len(content), b"path": [name.encode()]}
                    for name, content in originals.items()
                ],
                b"pieces": b"".join(
                    hashlib.sha1(payload[pos : pos + 16384]).digest()
                    for pos in range(0, len(payload), 16384)
                ),
            }
        }
    )
    descriptor = await inspect_torrent(raw)
    release = MAMRelease(
        source_id="901",
        title="Harbor collection",
        raw_title="Harbor collection",
        authors=["Alex Morgan"],
        medium="ebook",
        language="en",
        formats=["epub"],
        size_bytes=len(payload),
        seeders=30,
        protocol="torrent",
        observed_at=datetime.now(UTC),
    )
    async with database() as db, db.begin():
        source = SourceConnection(
            key="mam",
            base_url="https://mam.test",
            encrypted_secrets=encrypt_secrets({"mam_id": "fixture"}),
        )
        downloader = Integration(
            kind="qbittorrent",
            name="Pack fixture",
            base_url="http://qbit.test",
            encrypted_secrets=encrypt_secrets({"username": "fixture", "password": "fixture"}),
            status="connected",
            credential_generation=1,
            config={
                "save_path": "/downloads",
                "category": "book-search",
                "mappings": [
                    {
                        "download_root": "/downloads",
                        "source_key": "fixture",
                        "source_path": str(route["source"]),
                    }
                ],
            },
        )
        db.add_all([source, downloader])
        await db.flush()
        artifact = SourceArtifact(
            owner_id=UUID(admin["id"]),
            source_key="mam",
            source_id="901",
            source_generation=1,
            sha256=descriptor.artifact_sha256,
            descriptor=descriptor.model_dump(mode="json"),
            encrypted_content=encrypt_secrets({"torrent": base64.b64encode(raw).decode()}),
            release_snapshot=release.model_dump(mode="json"),
        )
        db.add(artifact)
        await db.flush()
        artifact_id, downloader_id = str(artifact.id), str(downloader.id)
    selections = []
    for index, book in enumerate([first] if manual_prepare else [first, second]):
        wanted = await request(
            client, body({"work": book["work"]}, "ebook", ebook_library_id=route["library_id"])
        )
        response = await prepare(
            client,
            {
                "intent_id": wanted["request"]["id"],
                "slot": "ebook",
                "artifact_id": artifact_id,
                "downloader_id": downloader_id,
                "downloader_generation": 1,
                "destination_id": route["destination"]["id"],
                "destination_revision": route["destination"]["revision"],
                "confirmed_work_id": str(book["work"]),
                **(
                    {"selected_paths": ["pack/book.epub", "pack/second.epub"]}
                    if selected_files
                    else {}
                ),
            },
            key=f"pack-selection-{index}",
        )
        assert response.status_code == 201, response.text
        selections.append(response.json())
    if reviewed_tags or swapped_review:
        # Freeze the per-book mapping produced by collection confirmation. The
        # files have title/author tags but no ISBN, so the import must exercise
        # that mapping and independently corroborate each book's embedded tags.
        async with database() as db, db.begin():
            paths = ["second.epub", "book.epub"] if swapped_review else ["book.epub", "second.epub"]
            for selected, name in zip(selections, paths, strict=True):
                saved = await db.get(AcquisitionSelection, UUID(selected["id"]))
                saved.frozen = {
                    **saved.frozen,
                    "collection_review": {"paths": [f"pack/{name}"]},
                }
    if manual_prepare:
        url = f"/api/acquisition/selections/{selections[0]['id']}"
        response = await client.get(url + "/pack-preview")
        assert response.status_code == 200, response.text
        preview = response.json()
        assert [r["work_id"] for r in preview["records"]] == [str(second["work"])], preview
        accepted = await client.post(
            url + "/pack-selections",
            headers={"Idempotency-Key": "manual-import-prepare"},
            json={"revision": preview["revision"], "work_ids": [str(second["work"])]},
        )
        assert accepted.status_code == 201, accepted.text
        selections.append(
            next(s for s in accepted.json()["selections"] if s["id"] != selections[0]["id"])
        )
        async with database() as db:
            assert not await db.scalar(select(DownloadAttempt.id))
    approval = await client.put(
        f"/api/organization/destinations/{route['destination']['id']}/automatic-import",
        json={
            "enabled": True,
            "expected_generation": 0,
            "destination_revision": route["destination"]["revision"],
        },
    )
    assert approval.status_code == 200 and approval.json()["ready"], approval.text
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)

    class SelectedClient(Client):
        async def submit(self, content, *, stopped=False, **kwargs):
            assert stopped
            receipt = await super().submit(content, **kwargs)
            self.states[0].state = "stoppedDL"
            for index, file in enumerate(self.states[0].files):
                file.index, file.priority = index, 1
            return receipt

        async def select_files(self, state, expected, paths):
            assert state.association_verified and state.state == "stoppedDL"
            assert {f.relative_path: f.size_bytes for f in state.files} == expected
            for file in self.states[0].files:
                file.priority = int(file.relative_path in paths)
                file.complete = bool(file.priority)
            self.calls.append("selected-files")

        async def start_transfer(self, key):
            async with database() as db:
                attempt = await db.scalar(select(DownloadAttempt))
                assert attempt.receipt["file_selection_start_attempted"]
            self.states[0].state = "uploading"
            self.calls.append("started-selected")

    qbit = (SelectedClient if selected_files else Client)(
        database, descriptor.model_dump(mode="json")
    )
    qbit.complete = True
    monkeypatch.setattr(downloads, "QbitClient", lambda *args: qbit)
    started = await grouped(client, *selections)
    assert started.status_code == 202, started.text
    if already_owned:
        # The first request is satisfied independently after selection/queueing.
        # This must not prevent the second book's continuation.
        from app.db.models import AssetContains, LibraryAsset

        async with database() as db, db.begin():
            asset = LibraryAsset(
                library_id=UUID(route["library_id"]),
                external_id="owned-ebook",
                version_id=first["version"],
                medium="ebook",
                state="present",
                full_content=True,
                match_status="matched",
            )
            db.add(asset)
            await db.flush()
            db.add(AssetContains(asset_id=asset.id, work_id=first["work"], verified=True))
    route["scan_backend"].detect = not delayed_scan
    await get_queue().run_worker_async(
        wait=False, concurrency=1, listen_notify=False, install_signal_handlers=False
    )
    async with database() as db:
        automatic = await db.scalar(select(AutomaticImport))
        if swapped_review:
            assert automatic and automatic.state == "held"
            reasons = [item["reason"] for item in automatic.evidence["held_groups"]]
            assert sum("different reviewed book" in reason for reason in reasons) == 2
            assert not await db.scalar(select(ImportEntry.id))
            assert not await db.scalar(select(DownloadFulfillment.id))
            assert not list(route["target"].rglob("*.epub"))
            return
        assert automatic and automatic.state == "importing", (
            automatic.message if automatic else "no import"
        )
        entries = list(await db.scalars(select(ImportEntry)))
        assert len(entries) == (1 if already_owned or ambiguous_first else 2), automatic.evidence
        if reviewed_tags:
            imported_versions = list(
                await db.scalars(
                    select(Version).where(Version.id.in_([e.version_id for e in entries]))
                )
            )
            by_work = {v.work_id: v.id for v in imported_versions}
            assert set(by_work) == {first["work"], second["work"]}
            assert all(not v.identifiers for v in imported_versions)
            first["version"], second["version"] = by_work[first["work"]], by_work[second["work"]]
        expected_versions = (
            {second["version"]}
            if already_owned or ambiguous_first
            else {first["version"], second["version"]}
        )
        assert {entry.version_id for entry in entries} == expected_versions
        assert bool(automatic.evidence["skipped_groups"]) is already_owned
        assert bool(automatic.evidence["held_groups"]) is not selected_files
        states = {entry.version_id: entry.state for entry in entries}
        assert states[second["version"]] == ("awaiting-library" if delayed_scan else "confirmed"), (
            states
        )
        if not already_owned and not ambiguous_first:
            assert states[first["version"]] == (
                "awaiting-library" if delayed_scan else "confirmed"
            ), states
    if delayed_scan:
        current = (await client.get(f"/api/acquisition/downloads/{started.json()['id']}")).json()
        assert not next(m for m in current["members"] if m["selection_id"] == selections[1]["id"])[
            "fulfillment"
        ]
        route["scan_backend"].detect = True
        route["scan_backend"].scan()
        for entry in entries:
            if entry.state == "awaiting-library":
                await execution.execute(entry.operation_id)
        await get_queue().run_worker_async(
            wait=False, concurrency=1, listen_notify=False, install_signal_handlers=False
        )
    if ambiguous_first:
        current = (await client.get(f"/api/acquisition/downloads/{started.json()['id']}")).json()
        first_member = next(
            m for m in current["members"] if m["selection_id"] == selections[0]["id"]
        )
        assert not first_member["fulfillment"] and first_member["target_state"] == "wanted"
        assert len(list(route["target"].rglob("*.epub"))) == 1
        second_inode = next(route["target"].rglob("*.epub")).stat().st_ino
        inspection_url = f"/api/organization/inspections/{current['inspection_id']}"
        inspection = (await client.get(inspection_url)).json()
        grouping = (await client.get(inspection_url + "/grouping")).json()
        group = next(
            g
            for g in grouping["content"]["groups"]
            if any(f["path"] == "book.epub" for f in g["files"])
        )
        settings = (await client.get("/api/organization/settings")).json()
        plan = await client.post(
            inspection_url + "/plans",
            json={
                "inspection_revision": inspection["snapshot"]["revision"],
                "grouping_revision": grouping["revision"],
                "profile_revision": settings["revision"],
                "selections": [
                    {
                        "group_key": group["key"],
                        "work_id": str(first["work"]),
                        "version_id": str(first["version"]),
                        "full_content": True,
                    }
                ],
            },
        )
        assert plan.status_code == 201, plan.text
        repaired = await client.post(
            f"/api/organization/plans/{plan.json()['id']}/imports",
            headers={"Idempotency-Key": "repair-only-ambiguous-child"},
            json={
                "plan_revision": plan.json()["revision"],
                "destinations": {
                    "ebook": {
                        "id": route["destination"]["id"],
                        "revision": route["destination"]["revision"],
                    }
                },
            },
        )
        assert repaired.status_code == 202, repaired.text
        await get_queue().run_worker_async(
            wait=False, concurrency=1, listen_notify=False, install_signal_handlers=False
        )
        assert second_inode in {path.stat().st_ino for path in route["target"].rglob("*.epub")}
    current = (await client.get(f"/api/acquisition/downloads/{started.json()['id']}")).json()
    assert all(
        m["fulfillment"] and m["fulfillment"]["available_now"] for m in current["members"]
    ), current
    assert qbit.calls.count("submit") == 1
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
        assert await db.scalar(select(func.count()).select_from(DownloadFulfillment)) == 2
        assert {r.state for r in await db.scalars(select(AcquisitionSelection))} == {"fulfilled"}
    imported = list(route["target"].rglob("*.epub"))
    assert len(imported) == (1 if already_owned else 2)
    source_inodes = {path.stat().st_ino for path in pack.glob("*.epub")}
    assert all(path.stat().st_ino in source_inodes for path in imported)
    assert {path.name: path.read_bytes() for path in pack.glob("*.epub")} == originals
    await get_queue().run_worker_async(
        wait=False, concurrency=1, listen_notify=False, install_signal_handlers=False
    )
    assert qbit.calls.count("submit") == 1 and len(list(route["target"].rglob("*.epub"))) == len(
        imported
    )


@pytest.mark.parametrize("delayed_scan", [False, True])
async def test_manual_pack_preview_creates_missing_requests_and_confirms_shared_import(
    client, admin, database, ready_route, monkeypatch, delayed_scan
):
    await test_reviewed_pack_imports_books_independently_and_preserves_seeded_files(
        client,
        admin,
        database,
        ready_route,
        monkeypatch,
        already_owned=False,
        delayed_scan=delayed_scan,
        ambiguous_first=False,
        manual_prepare=True,
    )


async def test_selected_pack_files_skip_existing_unrequested_bytes_end_to_end(
    client, admin, database, ready_route, monkeypatch
):
    await test_reviewed_pack_imports_books_independently_and_preserves_seeded_files(
        client, admin, database, ready_route, monkeypatch, False, False, False, selected_files=True
    )


async def test_reviewed_pack_without_isbns_imports_each_corroborated_book_end_to_end(
    client, admin, database, ready_route, monkeypatch
):
    await test_reviewed_pack_imports_books_independently_and_preserves_seeded_files(
        client,
        admin,
        database,
        ready_route,
        monkeypatch,
        False,
        False,
        False,
        selected_files=True,
        reviewed_tags=True,
    )


async def test_existing_catalog_matches_cannot_bypass_reviewed_book_file_mapping(
    client, admin, database, ready_route, monkeypatch
):
    await test_reviewed_pack_imports_books_independently_and_preserves_seeded_files(
        client,
        admin,
        database,
        ready_route,
        monkeypatch,
        False,
        False,
        False,
        swapped_review=True,
    )
