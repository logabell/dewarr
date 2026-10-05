# ruff: noqa: F811
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.adapters.audiobookshelf import ABSFile, ABSItem
from app.adapters.contracts import AdapterError, FailureKind
from app.config import get_settings
from app.db.models import (
    EbookCompanion,
    ImportDestination,
    ImportEntry,
    Integration,
    Library,
    LibraryAsset,
    Version,
    Work,
)
from app.domain.inventory import apply_item
from app.importing import colocate
from app.importing.destinations import destination_configuration
from app.importing.filesystem import directory
from app.importing.naming import fingerprint
from app.importing.publication import object_id, private_staging, publication_lock
from app.jobs.queue import get_queue
from tests.integration.test_import_execution import (  # noqa: F401
    destination_route,
    ready_route,
    start,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "enabled,revoked,interruption",
    [
        (True, False, None),
        (False, False, None),
        (True, True, None),
        (True, False, "scan"),
        (True, False, "scan-source-removed"),
        (True, False, "scan-source-route-disabled"),
        (True, False, "busy"),
    ],
)
async def test_confirmed_import_job_obeys_real_settings_and_verified_route(
    client,
    admin,
    database,
    ready_route,
    monkeypatch,
    enabled,
    revoked,  # noqa: F811
    interruption,
):
    route = ready_route
    monkeypatch.setattr(
        get_settings(), "import_destinations", {"ebooks": route["target"], "audio": route["target"]}
    )
    folder = route["target"] / "Narration"
    folder.mkdir()
    (folder / "Book.m4b").write_bytes(b"recording")
    async with database() as db, db.begin():
        ebook_route = await db.get(ImportDestination, UUID(route["destination"]["id"]))
        ebook_version = await db.scalar(select(Version).where(Version.medium == "ebook"))
        work = await db.get(Work, ebook_version.work_id)
        title, authors = work.title, work.authors
        audio_version = Version(work_id=ebook_version.work_id, medium="audio", narrators=["Reader"])
        audio_route = ImportDestination(
            root_key="audio",
            library_id=ebook_route.library_id,
            medium="audio",
            backend_path="/books",
            enabled=True,
        )
        db.add_all([audio_version, audio_route])
        await db.flush()
        config = await destination_configuration(db, audio_route)
        # Reuse the real probe's verified shared physical/backend mapping, bound
        # to the audio route's own configuration revision.
        audio_route.probe = {**ebook_route.probe, "configuration_revision": fingerprint(config)}
        if "download_routes" in audio_route.probe:
            audio_route.probe = {
                **audio_route.probe,
                "download_routes": [
                    {**item, "configuration_revision": fingerprint(config)}
                    for item in audio_route.probe["download_routes"]
                ],
            }
        if revoked:
            audio_route.probe = None
        db.add(
            LibraryAsset(
                library_id=ebook_route.library_id,
                external_id="existing-audio",
                version_id=audio_version.id,
                medium="audio",
                state="present",
                match_status="matched",
                full_content=True,
                files=[{"path": "/books/Narration/Book.m4b", "format": "m4b", "size": 9}],
                metadata_snapshot={"path": "/books/Narration"},
            )
        )
    # Adding the shared audio route changes naming semantics. Review a fresh
    # plan through the public API, just as the import UI does.
    inspection = (
        await client.get(f"/api/organization/inspections/{route['plan']['inspection_id']}")
    ).json()
    naming = (await client.get("/api/organization/settings")).json()
    original = route["plan"]["document"]["groups"][0]
    plan = await client.post(
        f"/api/organization/inspections/{inspection['id']}/plans",
        json={
            "inspection_revision": inspection["snapshot"]["revision"],
            "profile_revision": naming["revision"],
            "destinations": {"ebook": route["destination"]["id"]},
            "selections": [
                {
                    "group_key": inspection["snapshot"]["groups"][0]["key"],
                    "work_id": original["work_id"],
                    "version_id": original["version_id"],
                    "full_content": True,
                }
            ],
        },
    )
    assert plan.status_code == 201, plan.text
    route["plan"], route["plan_id"] = plan.json(), plan.json()["id"]
    response = await start(client, route)
    assert response.status_code == 202, response.text
    settings = (await client.get("/api/organization/settings")).json()
    saved = await client.put(
        "/api/organization/settings",
        json={
            "profile": {**settings["profile"], "ebooks_with_audio": enabled},
            "expected_revision": settings["revision"],
        },
    )
    assert saved.status_code == 200, saved.text
    interrupted = False
    observed = []

    def companion_backend(url, token):
        adapter = route["scan_backend"].client(url, token)
        scan = adapter.scan

        async def scan_with_interruption(external):
            nonlocal interrupted
            if interruption and interruption.startswith("scan") and not interrupted:
                interrupted = True
                raise AdapterError(FailureKind.UNAVAILABLE, "Synthetic transient scan failure")
            await scan(external)
            copy = folder / "First Harbor.epub"
            audio = ABSFile(path="/books/Narration/Book.m4b", size=9, format="m4b")
            ebook = ABSFile(
                path=f"/books/Narration/{copy.name}", size=copy.stat().st_size, format="epub"
            )
            observed.append(
                ABSItem(
                    id="existing-audio",
                    library_id=external,
                    title=title,
                    authors=authors,
                    narrators=["Reader"],
                    path="/books/Narration",
                    audio=[audio],
                    ebook=[ebook],
                    library_files=[audio, ebook],
                    full_audio=True,
                    full_ebook=False,
                    ebook_supplementary=True,
                )
            )

        adapter.scan = scan_with_interruption
        return adapter

    place = colocate.place_companion

    def contended_place(identifier, configuration, expected, **kwargs):
        nonlocal interrupted
        if interruption == "busy" and not interrupted:
            interrupted = True
            target = configuration["target"]
            with (
                directory(Path(target["root_path"])) as root,
                private_staging(
                    Path(target["staging_path"]),
                    Path(target["journal_path"]) if target.get("journal_path") else None,
                ) as stage,
                publication_lock(stage, json.dumps(object_id(root), sort_keys=True)),
            ):
                return place(identifier, configuration, expected, **kwargs)
        return place(identifier, configuration, expected, **kwargs)

    monkeypatch.setattr(colocate, "place_companion", contended_place)
    monkeypatch.setattr("app.adapters.audiobookshelf.Audiobookshelf", companion_backend)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    if interruption:
        assert interrupted and not observed
        async with database() as db, db.begin():
            job = (
                await db.execute(
                    text(
                        "SELECT id, status, attempts FROM book_queue.procrastinate_jobs "
                        "WHERE task_name = 'organization.ebook-companions'"
                    )
                )
            ).one()
            assert job.status == "todo" and job.attempts == 1
            row = await db.scalar(select(EbookCompanion))
            assert row.state == ("pending" if interruption == "busy" else "present")
            if interruption == "scan-source-removed":
                source = await db.get(LibraryAsset, row.source_asset_id)
                source.state = "intentionally-removed"
            elif interruption == "scan-source-route-disabled":
                source_route = await db.get(ImportDestination, UUID(route["destination"]["id"]))
                source_route.enabled = False
            await db.execute(
                text("UPDATE book_queue.procrastinate_jobs SET scheduled_at=now() WHERE id=:id"),
                {"id": job.id},
            )
        await get_queue().run_worker_async(wait=False, concurrency=1)
        assert len(observed) == 1
        async with database() as db, db.begin():
            library = await db.get(Library, ebook_route.library_id)
            await apply_item(db, library, observed[0], 2, library.integration_id, {observed[0].id})
            await db.flush()
            copy = await db.scalar(
                select(LibraryAsset).where(
                    LibraryAsset.external_id == "existing-audio", LibraryAsset.medium == "ebook"
                )
            )
            assert copy.full_content and copy.match_status == "matched"
            assert copy.version_id == ebook_version.id
    async with database() as db:
        entry = await db.scalar(
            select(ImportEntry).where(ImportEntry.run_id == UUID(response.json()["id"]))
        )
        assert entry.state == "confirmed", entry.message
        rows = list(await db.scalars(select(EbookCompanion)))
        jobs = await db.scalar(
            text(
                "SELECT count(*) FROM book_queue.procrastinate_jobs "
                "WHERE task_name = 'organization.ebook-companions'"
            )
        )
        assert bool(jobs) is enabled
    assert len(rows) == (1 if enabled and not revoked else 0)
    assert (folder / "First Harbor.epub").exists() is (enabled and not revoked)
    if enabled and not revoked:
        # A census holding the backend lock must still obtain the library lock
        # while companion preparation waits for that same backend. Reversing
        # this order deadlocks ordinary inventory against companion publication.
        waiting_for_backend = asyncio.Event()

        async def companion_context():
            async with database() as context_db, context_db.begin():
                get = context_db.get

                async def observe_get(model, *args, **kwargs):
                    if model is Integration and kwargs.get("with_for_update"):
                        waiting_for_backend.set()
                    return await get(model, *args, **kwargs)

                monkeypatch.setattr(context_db, "get", observe_get)
                return await colocate._context(context_db, entry.id)

        pending = None
        try:
            async with database() as census_db, census_db.begin():
                library = await census_db.get(Library, ebook_route.library_id)
                await census_db.get(Integration, library.integration_id, with_for_update=True)
                pending = asyncio.create_task(companion_context())
                await asyncio.wait_for(waiting_for_backend.wait(), timeout=5)
                await census_db.get(Library, library.id, with_for_update={"nowait": True})
            assert await asyncio.wait_for(pending, timeout=5)
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    if enabled and not revoked and interruption is None:
        # A route may be edited after _context first reads the entry's
        # destination but before it locks the current routes. The locked read
        # must replace that earlier identity-map snapshot, including its probe.
        async with database() as db, db.begin():
            companion = await db.get(EbookCompanion, rows[0].id)
            companion.state = "pending"
        context = colocate._context
        edited = False

        async def context_during_route_edit(db, entry_id):
            get = db.get

            async def edit_after_destination_read(model, *args, **kwargs):
                nonlocal edited
                if model is Integration and kwargs.get("with_for_update") and not edited:
                    async with database() as writer, writer.begin():
                        destination = await writer.get(
                            ImportDestination, ebook_route.id, with_for_update=True
                        )
                        destination.backend_path = "/changed-books"
                        destination.probe = None
                    edited = True
                return await get(model, *args, **kwargs)

            monkeypatch.setattr(db, "get", edit_after_destination_read)
            return await context(db, entry_id)

        def unexpected_placement(*args, **kwargs):
            pytest.fail("A concurrently revoked route must not reach filesystem placement")

        monkeypatch.setattr(colocate, "_context", context_during_route_edit)
        monkeypatch.setattr(colocate, "place_companion", unexpected_placement)
        copy = folder / "First Harbor.epub"
        before = copy.read_bytes()
        await colocate._place(entry.id, rows[0].id)
        assert edited and copy.read_bytes() == before
        async with database() as db:
            companion = await db.get(EbookCompanion, rows[0].id)
            assert companion.state == "held"
            assert companion.message == "Library routing changed; review ebook placement"


@pytest.mark.parametrize("first", ["ebook", "audio"])
async def test_either_import_order_places_ebook_in_each_narration_and_tracks_same_edition(
    database, tmp_path, monkeypatch, first
):
    source, target, stage = (tmp_path / name for name in ("ebooks", "audio", "stage"))
    source.mkdir()
    target.mkdir()
    stage.mkdir(mode=0o700)
    (source / "Book.epub").write_bytes(b"complete ebook")
    for name in ("Reader One", "Reader Two", "Conflicting"):
        (target / name).mkdir()
        (target / name / "Book.m4b").write_bytes(b"recording")
    (target / "Conflicting" / "Book.epub").write_bytes(b"existing different ebook")
    source_id, target_id = str(uuid4()), str(uuid4())
    configs = {
        source_id: {
            "medium": "ebook",
            "root_path": str(source),
            "backend_path": "/ebooks",
            "staging_path": str(stage),
        },
        target_id: {
            "medium": "audio",
            "root_path": str(target),
            "backend_path": "/audio",
            "staging_path": str(stage),
        },
    }
    async with database() as db, db.begin():
        integration = Integration(
            kind="audiobookshelf",
            name="Test",
            base_url="http://test",
            encrypted_secrets="unused",
            enabled=True,
        )
        work = Work(title="Book", authors=["Writer"])
        db.add_all([integration, work])
        await db.flush()
        library = Library(integration_id=integration.id, external_id="combined", name="Combined")
        ebook = Version(work_id=work.id, medium="ebook")
        audio_versions = [
            Version(work_id=work.id, medium="audio", narrators=[name])
            for name in ("Reader One", "Reader Two", "Conflicting")
        ]
        db.add_all([library, ebook, *audio_versions])
        await db.flush()
        source_asset = LibraryAsset(
            library_id=library.id,
            external_id="canonical",
            version_id=ebook.id,
            medium="ebook",
            state="present",
            match_status="matched",
            full_content=True,
            files=[
                {
                    "path": "/ebooks/Book.epub",
                    "format": "epub",
                    "size": (source / "Book.epub").stat().st_size,
                }
            ],
            metadata_snapshot={"path": "/ebooks"},
        )
        audio_assets = [
            LibraryAsset(
                library_id=library.id,
                external_id=f"audio-{index}",
                version_id=version.id,
                medium="audio",
                state="present",
                match_status="matched",
                full_content=True,
                files=[
                    {"path": f"/audio/{version.narrators[0]}/Book.m4b", "format": "m4b", "size": 9}
                ],
                metadata_snapshot={"path": f"/audio/{version.narrators[0]}"},
            )
            for index, version in enumerate(audio_versions)
        ]
        ids = (library.id, integration.id, work.id, ebook.id)
        db.add_all([source_asset] if first == "ebook" else audio_assets)
    entry = SimpleNamespace(message="Available")

    async def context(db, entry_id):
        return (
            entry,
            await db.get(Library, ids[0]),
            await db.get(Integration, ids[1]),
            await db.get(Work, ids[2]),
            configs,
        )

    scans = []

    class Backend:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scan(self, external):
            scans.append(external)

    monkeypatch.setattr(colocate, "_context", context)
    monkeypatch.setattr("app.adapters.audiobookshelf.Audiobookshelf", Backend)
    monkeypatch.setattr("app.security.decrypt_secrets", lambda _: {"token": "unused"})
    trigger = uuid4()
    await colocate.reconcile(trigger)
    assert not (target / "Reader One/Book.epub").exists()
    async with database() as db, db.begin():
        db.add_all(audio_assets if first == "ebook" else [source_asset])
    # Exhaust one page while hashing its first file. The intent and continuation
    # must commit before another run gets a fresh budget.
    read_source = colocate.source_evidence

    def exhausted(config, *, deadline=None):
        raise colocate.PlacementBudgetExpired("Synthetic exhausted page budget")

    monkeypatch.setattr(colocate, "source_evidence", exhausted)
    await colocate.reconcile(trigger)
    async with database() as db:
        pending = await db.scalar(select(EbookCompanion).where(EbookCompanion.library_id == ids[0]))
        assert pending.state == "pending" and pending.receipt["budget_retry"]
        queued = await db.scalar(
            text(
                "SELECT args FROM book_queue.procrastinate_jobs "
                "WHERE task_name = 'organization.ebook-companions'"
            )
        )
        assert queued == {"entry_id": str(trigger), "after": None}
    monkeypatch.setattr(colocate, "source_evidence", read_source)
    await colocate.reconcile(trigger)
    await colocate.reconcile(trigger)
    async with database() as db:
        rows = list(
            await db.scalars(select(EbookCompanion).where(EbookCompanion.library_id == ids[0]))
        )
    assert len(rows) == 3
    assert {row.version_id for row in rows} == {ids[3]}
    assert sorted(row.state for row in rows) == ["held", "present", "present"]
    for name in ("Reader One", "Reader Two"):
        assert (target / name / "Book.epub").read_bytes() == (source / "Book.epub").read_bytes()
    assert (target / "Conflicting/Book.epub").read_bytes() == b"existing different ebook"
    assert scans == ["combined", "combined"]
    # A later inventory move must not publish into the formerly owned folder,
    # even while the same target asset still belongs to the correct work.
    known = next(row for row in rows if row.state == "present")
    old_path = target / known.configuration["target_relative"]
    old_path.unlink()
    async with database() as db, db.begin():
        destination_asset = await db.get(LibraryAsset, known.target_asset_id)
        destination_asset.metadata_snapshot = {"path": "/audio/Moved narration"}
        tracked = await db.get(EbookCompanion, known.id)
        tracked.state = "pending"
    await colocate._place(trigger, known.id)
    async with database() as db:
        assert (await db.get(EbookCompanion, known.id)).state == "held"
    assert not old_path.exists()
