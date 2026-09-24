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
from tests.filesystem_fixtures import path_bound_directory_handles  # noqa: F401

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
            encrypted_secrets="never-contact-downloader",
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


@pytest.mark.parametrize("kind", ["qbittorrent", "slskd"])
async def test_empty_folder_can_qualify_before_any_plan_or_download(
    client, admin, database, empty_route, kind
):
    async with database() as db, db.begin():
        (await db.get(Integration, empty_route["downloader"])).kind = kind
    responses = await asyncio.gather(*(start(client, empty_route) for _ in range(3)))
    assert all(item.status_code == 202 for item in responses)
    assert len({item.json()["id"] for item in responses}) == 1
    await get_queue().run_worker_async(wait=False, concurrency=1)
    checked = await current(client)
    assert checked["publication_available"]
    assert checked["probe"]["hardlink"] and checked["probe"]["backend"]["root_mapping"]
    assert checked["probe"]["setup_downloader"]["mapping"]["relative_path"] == "books"
    assert not list((empty_route["source"] / "books").iterdir())
    assert not list(empty_route["staging"].iterdir()) and not list(empty_route["target"].iterdir())
    options = (await client.get("/api/acquisition/selections/options")).json()
    assert options["downloaders"][0]["ready"] and options["destinations"][0]["ready"]
    policy = (
        await client.get(f"/api/organization/destinations/{checked['id']}/automatic-import")
    ).json()
    assert policy["can_enable"] and not policy["enabled"]
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
