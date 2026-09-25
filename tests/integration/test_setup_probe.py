import asyncio
import errno
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import FrozenImportPlan, Integration, Library, Operation, User
from app.importing import destinations, publication
from app.importing.filesystem import identity
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.abs_import_fixture import ImportBackendFixture
from tests.filesystem_fixtures import (
    path_bound_directory_handles,  # noqa: F401
    read_only_downloads,  # noqa: F401
    smb_open_children,  # noqa: F401
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def empty_route(client, admin, database, tmp_path, monkeypatch):
    source, target, staging = (
        tmp_path.resolve() / name for name in ("downloads", "library", "stage")
    )
    for root in (source, target, staging):
        root.mkdir(mode=0o700)
    (source / "books").mkdir()
    backend = ImportBackendFixture(target)
    monkeypatch.setattr(destinations, "Audiobookshelf", backend.client)
    monkeypatch.setattr(get_settings(), "import_sources", {"fixture": source})
    monkeypatch.setattr(get_settings(), "import_destinations", {"ebooks": target})
    monkeypatch.setattr(get_settings(), "import_staging_root", staging)
    async with database() as db, db.begin():
        connection = Integration(
            name="ABS",
            kind="audiobookshelf",
            base_url="http://fixture",
            encrypted_secrets=encrypt_secrets({"token": "private-import-token"}),
            status="connected",
            enabled=True,
        )
        downloader = Integration(
            name="qBit",
            kind="qbittorrent",
            base_url="http://unused.invalid",
            encrypted_secrets=encrypt_secrets({"password": "never-contact-downloader"}),
            status="connected",
            enabled=True,
            credential_generation=1,
            config={
                "save_path": "/downloads/books",
                "category": "book-search",
                "mappings": [
                    {
                        "download_root": "/downloads",
                        "source_key": "fixture",
                        "source_path": str(source),
                    }
                ],
            },
        )
        db.add_all([connection, downloader])
        await db.flush()
        library = Library(
            integration_id=connection.id,
            external_id="synthetic",
            name="Ebooks",
            accessible=True,
            last_complete_sync=datetime.now(UTC),
        )
        db.add(library)
        await db.flush()
        library_id, downloader_id = library.id, downloader.id
    saved = await client.put(
        "/api/organization/destinations/ebooks",
        json={
            "library_id": str(library_id),
            "medium": "ebook",
            "backend_path": "/books",
        },
    )
    assert saved.status_code == 200, saved.text
    return {
        "destination": saved.json(),
        "downloader": downloader_id,
        "source": source,
        "target": target,
        "staging": staging,
        "backend": backend,
    }


async def start(client, route, key="empty-folder-probe", **overrides):
    return await client.post(
        f"/api/organization/destinations/{route['destination']['id']}/setup-probe",
        headers={"Idempotency-Key": key},
        json={
            "downloader_id": str(route["downloader"]),
            "downloader_generation": 1,
            "expected_revision": route["destination"]["revision"],
            **overrides,
        },
    )


async def current(client):
    response = await client.get("/api/organization/destinations")
    assert response.status_code == 200
    return response.json()[0]


@pytest.mark.parametrize("missing", ["root", "save_folder"])
async def test_missing_soulseek_download_folder_reports_mapped_path(
    client, admin, database, empty_route, missing
):
    async with database() as db, db.begin():
        (await db.get(Integration, empty_route["downloader"])).kind = "slskd"
    (empty_route["source"] / "books").rmdir()
    if missing == "root":
        empty_route["source"].rmdir()
    response = await start(client, empty_route)
    assert response.status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert not checked["publication_available"]
    async with database() as db:
        operation = await db.get(Operation, UUID(response.json()["id"]))
        assert operation.status == "failed"
        assert "Download folder" in operation.message
        assert str(empty_route["source"] / "books") in operation.message
        assert "ENOENT" in operation.message
        assert str(empty_route["target"]) not in operation.message


@pytest.mark.parametrize("kind", ["qbittorrent", "slskd"])
@pytest.mark.parametrize("filesystem", ["local", "smb", "read-only"])
async def test_empty_folder_can_qualify_before_any_plan_or_download(
    client, admin, database, empty_route, kind, filesystem, request
):
    if filesystem == "smb":
        request.getfixturevalue("smb_open_children")
    elif filesystem == "read-only":
        request.getfixturevalue("read_only_downloads")(empty_route["source"] / "books")
    async with database() as db, db.begin():
        (await db.get(Integration, empty_route["downloader"])).kind = kind
    responses = await asyncio.gather(*(start(client, empty_route) for _ in range(3)))
    assert all(item.status_code == 202 for item in responses)
    assert len({item.json()["id"] for item in responses}) == 1
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert checked["publication_available"]
    assert checked["probe"]["hardlink"] is (filesystem != "read-only")
    assert checked["probe"]["backend"]["root_mapping"]
    if filesystem == "read-only":
        assert checked["mode"] == "copy" and checked["probe"]["copy"]
        assert checked["probe"]["source_readable"] and not checked["probe"]["source_writable"]
        assert "read-only to Dewarr" in checked["probe"]["message"]
    assert checked["probe"]["setup_downloader"]["mapping"]["relative_path"] == "books"
    assert not list((empty_route["source"] / "books").iterdir())
    assert not list(empty_route["staging"].iterdir()) and not list(empty_route["target"].iterdir())
    options = (await client.get("/api/acquisition/selections/options")).json()
    assert options["downloaders"][0]["ready"] and options["destinations"][0]["ready"]
    policy = (
        await client.get(f"/api/organization/destinations/{checked['id']}/automatic-import")
    ).json()
    assert policy["can_enable"] and policy["enabled"] and policy["ready"]
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(FrozenImportPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(Operation)) == 1
    assert (await start(client, empty_route)).json()["id"] == responses[0].json()["id"]
    assert (await start(client, empty_route, downloader_generation=2)).status_code == 409


@pytest.mark.parametrize("change", ["generation", "disabled", "path", "mapping"])
async def test_changed_downloader_invalidates_probe_and_selection_options(
    client, admin, database, empty_route, change
):
    assert (await start(client, empty_route)).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert (await current(client))["publication_available"]
    async with database() as db, db.begin():
        row = await db.get(Integration, empty_route["downloader"])
        if change == "generation":
            row.credential_generation += 1
        elif change == "disabled":
            row.enabled = False
        elif change == "path":
            row.config = {**row.config, "save_path": "/downloads/changed"}
        else:
            row.config = {
                **row.config,
                "mappings": [{**row.config["mappings"][0], "source_path": "/changed"}],
            }
    assert not (await current(client))["publication_available"]
    options = (await client.get("/api/acquisition/selections/options")).json()
    assert not options["destinations"][0]["ready"]


@pytest.mark.parametrize("when", ["before", "during"])
async def test_late_settings_change_discards_setup_result(
    client, admin, database, empty_route, when
):
    response = await start(client, empty_route)
    assert response.status_code == 202

    async def change(_=None):
        async with database() as db, db.begin():
            row = await db.get(Integration, empty_route["downloader"])
            row.credential_generation += 1

    if when == "before":
        await change()
    else:
        empty_route["backend"].before_exists = change
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert not (await current(client))["publication_available"]
    async with database() as db:
        op = await db.get(Operation, UUID(response.json()["id"]))
        assert op.status == "failed"
    assert not list((empty_route["source"] / "books").iterdir())
    assert not list(empty_route["target"].iterdir())


async def test_setup_probe_requires_fresh_settings_and_admin_consent(
    client, admin, database, empty_route
):
    assert (await start(client, empty_route, downloader_generation=2)).status_code == 409
    assert (await start(client, empty_route, expected_revision="0" * 64)).status_code == 409
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await start(client, empty_route)).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "member"
    assert (await start(client, empty_route)).status_code == 403
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0


@pytest.mark.parametrize("failure", ["missing-folder", "symlink", "audiobooks-only"])
async def test_failed_setup_does_not_touch_downloaded_files(
    client, admin, empty_route, failure, tmp_path
):
    saved = empty_route["source"] / "keep.epub"
    saved.write_bytes(b"existing download")
    before = identity(saved.stat())
    folder = empty_route["source"] / "books"
    if failure == "missing-folder":
        folder.rmdir()
    elif failure == "symlink":
        folder.rmdir()
        folder.symlink_to(tmp_path)
    else:
        empty_route["backend"].settings["audiobooksOnly"] = True
    assert (await start(client, empty_route)).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert not checked["publication_available"] and checked["probe"]["status"] == "failed"
    assert saved.read_bytes() == b"existing download" and identity(saved.stat()) == before
    assert not list(empty_route["target"].iterdir()) and not list(empty_route["staging"].iterdir())
    assert not list(tmp_path.rglob(".book-search-route-*"))


@pytest.mark.parametrize("mode", ["hardlink", "copy"])
@pytest.mark.parametrize("link_error", [errno.EXDEV, errno.EPERM])
@pytest.mark.usefixtures("path_bound_directory_handles")
async def test_cross_filesystem_route_copies_when_hardlink_is_impossible(
    client, admin, empty_route, monkeypatch, mode, link_error
):
    def cross_device(*args, **kwargs):
        raise OSError(link_error, "hardlinks unavailable")

    monkeypatch.setattr(publication.os, "link", cross_device)
    saved = empty_route["destination"]
    response = await client.put(
        "/api/organization/destinations/ebooks",
        json={
            "library_id": saved["library_id"],
            "medium": "ebook",
            "backend_path": "/books",
            "mode": mode,
            "expected_revision": saved["revision"],
        },
    )
    assert response.status_code == 200
    empty_route["destination"] = response.json()
    assert (await start(client, empty_route)).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert checked["publication_available"]
    assert checked["mode"] == "copy"
    assert not checked["probe"]["hardlink"] and checked["probe"]["copy"]
    assert "copied into the library" in checked["probe"]["message"]
    activated = await client.post(
        f"/api/organization/library-folders/{checked['id']}/activate",
        json={"expected_revision": checked["revision"]},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["mode"] == "copy" and activated.json()["publication_available"]


async def test_seeding_rename_is_optional_and_does_not_require_a_hardlink(
    client, admin, empty_route, monkeypatch
):
    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "different filesystem")

    seen = {}

    async def confirm(downloader_id, client_path, worker_root, *, client_factory=None):
        seen["downloader_id"] = str(downloader_id)
        seen["client_path"] = client_path
        seen["worker_root"] = worker_root

    monkeypatch.setattr(publication.os, "link", cross_device)
    monkeypatch.setattr(destinations, "confirm_library_mapping", confirm)
    saved = empty_route["destination"]
    assert saved["seeding_rename"] is False
    response = await client.put(
        "/api/organization/destinations/ebooks",
        json={
            "library_id": saved["library_id"],
            "medium": "ebook",
            "backend_path": "/books",
            "seeding_rename": True,
            "client_path": "/library/books",
            "expected_revision": saved["revision"],
        },
    )
    assert response.status_code == 200, response.text
    empty_route["destination"] = response.json()
    assert empty_route["destination"]["seeding_rename"] is True
    assert empty_route["destination"]["client_path"] == "/library/books"
    assert empty_route["destination"]["mode"] == "hardlink"
    assert (await start(client, empty_route, key="seeding-rename-probe")).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert checked["publication_available"], checked
    assert checked["mode"] == "hardlink" and checked["seeding_rename"] is True
    assert checked["probe"]["seeding_rename"] and not checked["probe"]["hardlink"]
    assert "same copy" in checked["probe"]["message"]
    assert seen == {
        "downloader_id": str(empty_route["downloader"]),
        "client_path": "/library/books",
        "worker_root": empty_route["target"],
    }
    activated = await client.post(
        f"/api/organization/library-folders/{checked['id']}/activate",
        json={"expected_revision": checked["revision"]},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["seeding_rename"] is True and activated.json()["publication_available"]


async def test_probe_operation_error_does_not_blame_missing_mount(
    client, admin, empty_route, monkeypatch
):
    real_move = publication.no_replace

    def fail_library_move(source_fd, source_name, destination_fd, destination_name):
        if source_name.startswith("probe-"):
            raise OSError(errno.ENOENT, "Synthetic rename failure")
        return real_move(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(publication, "no_replace", fail_library_move)
    assert (await start(client, empty_route)).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert not checked["publication_available"]
    assert checked["probe"]["status"] == "failed"
    assert checked["probe"]["failure_step"] == "checking safe library publication"
    assert "(ENOENT)" in checked["probe"]["message"]
    assert "folders were opened successfully" in checked["probe"]["message"]
    assert not list(empty_route["staging"].iterdir())
    assert not list(empty_route["target"].iterdir())


async def test_probe_cleanup_failure_cannot_activate_and_keeps_diagnostics(
    client, admin, empty_route, monkeypatch
):
    real_rmdir = publication.os.rmdir

    def denied(path, *, dir_fd=None):
        if str(path).startswith(".book-search-probe-"):
            raise OSError(errno.EACCES, "Storage denied temporary folder cleanup")
        return real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(publication.os, "rmdir", denied)
    assert (await start(client, empty_route)).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert not checked["publication_available"]
    report = checked["probe"]
    assert report["status"] == "failed"
    assert report["failure_step"] == "cleaning up temporary probe files"
    assert report["error_code"] == "EACCES"
    assert "EACCES" in report["message"]
    assert report["cleanup_failures"][0]["path"].startswith(str(empty_route["target"]))
    activated = await client.post(
        f"/api/organization/library-folders/{checked['id']}/activate",
        json={"expected_revision": checked["revision"]},
    )
    assert activated.status_code == 409


@pytest.mark.parametrize("failure", ["disabled", "failed_probe", "changed_during_probe"])
async def test_clients_share_library_without_replacing_defaults_or_each_others_verification(
    client, admin, database, empty_route, monkeypatch, failure
):
    from app.db.models import AutomaticImportPolicy, ImportDestination
    from app.domain.acquisition_selection import verified_probe
    from app.importing.route_evidence import approved
    from tests.integration.test_acquisition_defaults import save

    route = empty_route
    settings = get_settings()
    sources = dict(settings.import_sources)
    client_ids = [route["downloader"]]
    async with database() as db, db.begin():
        for kind in ("sabnzbd", "slskd"):
            root = route["source"].parent / kind
            (root / "books").mkdir(parents=True)
            sources[kind] = root
            row = Integration(
                name=kind,
                kind=kind,
                base_url="http://unused.invalid",
                encrypted_secrets=encrypt_secrets({"password": "never-contact-downloader"}),
                enabled=True,
                status="connected",
                credential_generation=1,
                config={
                    "save_path": "/downloads/books",
                    "category": "books",
                    "mappings": [
                        {
                            "download_root": "/downloads",
                            "source_key": kind,
                            "source_path": str(root),
                        }
                    ],
                },
            )
            db.add(row)
            await db.flush()
            client_ids.append(row.id)
    monkeypatch.setattr(settings, "import_sources", sources)
    await save(client, {"downloader_id": str(client_ids[0])}, "installation")
    monkeypatch.setattr(settings, "download_dispatch_enabled", True)
    initial_generation = None
    for index, client_id in enumerate(client_ids):
        response = await start(client, route, key=f"client-{index}", downloader_id=str(client_id))
        assert response.status_code == 202, response.text
        await get_queue().run_worker_async(wait=False, concurrency=1)
        # Each verified client works immediately, without enabling imports again
        # or invalidating downloads authorized before this client was added.
        from app.domain.automatic_dispatch import approve_route

        checked = await current(client)
        async with database() as db:
            policy = await db.scalar(select(AutomaticImportPolicy))
            initial_generation = initial_generation or policy.generation
            assert policy.enabled and policy.generation == initial_generation
            mapping = checked["probe"]["setup_downloader"]["mapping"]
            await approve_route(
                db, UUID(admin["id"]), UUID(checked["id"]), checked["revision"], mapping=mapping
            )
    checked = await current(client)
    assert checked["publication_available"]
    assert len(checked["probe"]["download_routes"]) == 3
    options = (await client.get("/api/acquisition/selections/options")).json()
    assert options["destinations"][0]["source_keys"] == ["fixture", "sabnzbd", "slskd"]
    response = await client.post(
        f"/api/organization/library-folders/{checked['id']}/activate",
        json={"expected_revision": checked["revision"]},
    )
    assert response.status_code == 200, response.text
    for scope in ("personal", "installation"):
        preferences = (await client.get(f"/api/acquisition/preferences/{scope}")).json()
        assert preferences["effective"]["downloader_id"] == str(client_ids[0])
    async with database() as db:
        policy = await db.scalar(select(AutomaticImportPolicy))
        destination = await db.get(ImportDestination, UUID(checked["id"]))
        configuration = await destinations.destination_configuration(db, destination)
        for source_key in sources:
            mapping = {"source_key": source_key, "relative_path": "books"}
            assert approved(policy.configuration, checked["probe"], mapping)
            assert await verified_probe(db, destination, configuration, mapping)
        assert not approved(
            policy.configuration,
            checked["probe"],
            {"source_key": "slskd", "relative_path": "unapproved"},
        )
    if failure != "disabled":
        if failure == "failed_probe":
            (sources["slskd"] / "books").rmdir()
        response = await start(
            client, route, key="recheck-soulseek", downloader_id=str(client_ids[-1])
        )
        assert response.status_code == 202, response.text
    if failure != "failed_probe":
        async with database() as db, db.begin():
            (await db.get(Integration, client_ids[-1])).enabled = False
    if failure != "disabled":
        await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert checked["publication_available"]
    assert {item["source_key"] for item in checked["probe"]["download_routes"]} == {
        "fixture",
        "sabnzbd",
    }
    policy = (
        await client.get(f"/api/organization/destinations/{checked['id']}/automatic-import")
    ).json()
    assert policy["ready"], policy


@pytest.mark.parametrize("enabled", [False, True])
async def test_setup_verification_honors_saved_import_preference(
    client, admin, database, empty_route, enabled
):
    destination = empty_route["destination"]
    endpoint = f"/api/organization/destinations/{destination['id']}/automatic-import"
    # Save the choice while verification is queued; the worker must use the latest choice.
    assert (await start(client, empty_route)).status_code == 202
    response = await client.put(
        endpoint,
        json={
            "enabled": enabled,
            "defer_until_verified": True,
            "expected_generation": 0,
            "destination_revision": destination["revision"],
        },
    )
    assert response.status_code == 200, response.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert (await current(client))["publication_available"]
    policy = (await client.get(endpoint)).json()
    assert policy["enabled"] is enabled
    assert policy["ready"] is enabled


async def test_mapping_save_automatically_rechecks_library(client, admin, database, empty_route):
    assert (await start(client, empty_route)).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    before = await current(client)
    assert before["client_routes"][0]["status"] == "verified"
    response = await client.put(
        f"/api/downloaders/{empty_route['downloader']}/mappings",
        json={
            "expected_generation": 1,
            "mappings": [{"download_root": "/downloads", "source_key": "fixture"}],
        },
    )
    assert response.status_code == 200, response.text
    stale = await current(client)
    assert not stale["publication_available"]
    assert stale["client_routes"][0]["status"] == "needs-verification"
    await get_queue().run_worker_async(wait=False, concurrency=1)
    after = await current(client)
    assert after["client_routes"][0]["status"] == "verified"
    assert after["client_routes"][0]["checked_at"]
    assert after["probe"]["setup_downloader"]["generation"] == 2
    policy = (
        await client.get(f"/api/organization/destinations/{after['id']}/automatic-import")
    ).json()
    assert policy["enabled"] and policy["ready"]


async def test_connection_test_verifies_all_clients_and_reports_each_failure(
    client, admin, database, empty_route, monkeypatch
):
    from app.domain import downloaders

    async def connected(*args):
        pass

    monkeypatch.setattr(downloaders, "test_connection", connected)
    async with database() as db, db.begin():
        other = Integration(
            name="SAB",
            kind="sabnzbd",
            base_url="http://unused.invalid",
            encrypted_secrets="unused",
            status="connected",
            enabled=True,
            credential_generation=1,
            config={
                "save_path": "/downloads/missing",
                "mappings": [
                    {
                        "download_root": "/downloads",
                        "source_key": "fixture",
                        "source_path": str(empty_route["source"]),
                    }
                ],
            },
        )
        db.add(other)
    response = await client.post(f"/api/downloaders/{empty_route['downloader']}/test")
    assert response.status_code == 200, response.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    routes = {route["name"]: route for route in checked["client_routes"]}
    assert checked["publication_available"]  # The working client remains usable.
    assert routes["qBit"]["status"] == "verified"
    assert routes["SAB"]["status"] == "failed"
    assert "missing" in routes["SAB"]["message"]
    (empty_route["source"] / "missing").mkdir()
    assert (
        await client.post(f"/api/downloaders/{empty_route['downloader']}/test")
    ).status_code == 200
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert all(route["status"] == "verified" for route in (await current(client))["client_routes"])


async def test_automatic_route_verification_resumes_after_worker_restart(
    client, admin, database, empty_route, monkeypatch
):
    from app.importing import setup_verification

    original = setup_verification.probe_route

    async def interrupted(operation_id):
        raise RuntimeError("worker interrupted")

    monkeypatch.setattr(setup_verification, "probe_route", interrupted)
    with pytest.raises(RuntimeError, match="worker interrupted"):
        await setup_verification.verify_download_routes(admin["id"], job_id=1234)
    assert (await current(client))["client_routes"][0]["status"] == "checking"
    monkeypatch.setattr(setup_verification, "probe_route", original)
    await setup_verification.verify_download_routes(admin["id"], job_id=1234)
    assert (await current(client))["client_routes"][0]["status"] == "verified"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 1
    # A duplicate setup event skips already-current routes.
    await setup_verification.verify_download_routes(admin["id"], job_id=1235)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 1
