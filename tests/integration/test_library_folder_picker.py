# ruff: noqa: F811
from uuid import UUID

import pytest
from sqlalchemy import select

from app.api import library_folders
from app.config import get_settings
from app.db.models import (
    AutomaticImportPolicy,
    ImportDestination,
    ImportEntry,
    ImportStorageSettings,
    Library,
)
from app.importing import destinations
from app.importing.storage import storage_settings
from app.jobs.queue import get_queue
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_destinations import start_probe
from tests.integration.test_import_execution import start as start_import
from tests.integration.test_setup_probe import empty_route, start  # noqa: F401

pytestmark = pytest.mark.integration


async def test_picker_persists_mounts_verifies_and_sets_both_defaults(
    client, admin, database, empty_route, monkeypatch
):
    route = empty_route
    monkeypatch.setattr(library_folders, "Audiobookshelf", route["backend"].client)
    monkeypatch.setattr(get_settings(), "import_destinations", {})
    monkeypatch.setattr(get_settings(), "import_staging_root", None)
    options = (await client.get("/api/organization/library-folders")).json()
    assert options[0]["folders"] == ["/books"]
    library_id = options[0]["library_id"]
    # Replace the prior explicitly configured destination using its current revision.
    existing = (await client.get("/api/organization/destinations")).json()[0]
    response = await client.put(
        "/api/organization/library-folders/ebook",
        json={
            "library_id": library_id,
            "backend_path": "/books",
            "local_path": str(route["target"]),
            "destination_id": existing["id"],
            "expected_revision": existing["revision"],
        },
    )
    assert response.status_code == 200, response.text
    chosen = response.json()
    assert chosen["configured"] and chosen["mode"] == "hardlink"
    assert chosen["seeding_rename"] is False
    assert chosen["local_path"] == str(route["target"])
    assert not chosen["publication_available"]
    denied = await client.post(
        f"/api/organization/library-folders/{chosen['id']}/activate",
        json={"expected_revision": chosen["revision"]},
    )
    assert denied.status_code == 409
    route["destination"] = chosen
    assert (await start(client, route)).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    verified = (await client.get("/api/organization/destinations")).json()[0]
    assert verified["publication_available"], verified
    assert (route["target"].parent / ".book-search-staging").is_dir()
    response = await client.post(
        f"/api/organization/library-folders/{chosen['id']}/activate",
        json={"expected_revision": chosen["revision"]},
    )
    assert response.status_code == 200, response.text
    for scope in ["personal", "installation"]:
        defaults = (await client.get(f"/api/acquisition/preferences/{scope}")).json()["effective"]
        assert defaults["ebook_library_id"] == library_id
        assert defaults["ebook_destination_id"] == chosen["id"]
        assert defaults["downloader_id"] == str(route["downloader"])
    async with database() as db:
        mounted = await storage_settings(db)
        assert mounted.import_destinations["ebooks"] == route["target"]
        assert (await db.scalar(select(AutomaticImportPolicy))).enabled
        assert await db.get(ImportStorageSettings, 1)
        assert (await db.get(ImportDestination, UUID(chosen["id"]))).probe["hardlink"]
    # Another valid media destination must not invalidate the first verified route.
    audio_target = route["target"].parent / "audiobooks"
    audio_target.mkdir()
    response = await client.put(
        "/api/organization/library-folders/audio",
        json={"library_id": library_id, "backend_path": "/books", "local_path": str(audio_target)},
    )
    assert response.status_code == 200, response.text
    saved = (await client.get("/api/organization/destinations")).json()
    assert next(d for d in saved if d["id"] == chosen["id"])["publication_available"]


async def test_picker_lists_older_abs_library_and_explains_failures(
    client, admin, empty_route, monkeypatch
):
    backend = empty_route["backend"]
    monkeypatch.setattr(library_folders, "Audiobookshelf", backend.client)
    backend.settings = {"coverAspectRatio": 1}
    options = (await client.get("/api/organization/library-folders")).json()
    assert options[0]["folders"] == ["/books"]
    assert options[0]["error"] is None
    backend.settings = {"disableWatcher": "false"}
    options = (await client.get("/api/organization/library-folders")).json()
    assert options[0]["folders"] == []
    assert options[0]["error"] == (
        "Could not read this library's folders. "
        "Audiobookshelf library import settings are incomplete or unsupported."
    )


async def test_picker_rejects_unknown_abs_folder_and_overlapping_mount(
    client, admin, empty_route, monkeypatch
):
    monkeypatch.setattr(library_folders, "Audiobookshelf", empty_route["backend"].client)
    body = {
        "library_id": empty_route["destination"]["library_id"],
        "backend_path": "/not-an-abs-folder",
        "local_path": str(empty_route["target"]),
    }
    assert (
        await client.put("/api/organization/library-folders/audio", json=body)
    ).status_code == 422
    body.update(backend_path="/books", local_path=str(empty_route["source"]))
    assert (
        await client.put("/api/organization/library-folders/audio", json=body)
    ).status_code == 422
    body["local_path"] = "/data/../etc"
    assert (
        await client.put("/api/organization/library-folders/audio", json=body)
    ).status_code == 422


async def save_staging(database, path):
    async with database() as db, db.begin():
        storage = await db.get(ImportStorageSettings, 1)
        if not storage:
            storage = ImportStorageSettings(id=1, destinations={}, sources={})
            db.add(storage)
        storage.staging_root = str(path)


async def saved_staging(database):
    async with database() as db:
        return (await db.get(ImportStorageSettings, 1)).staging_root


def separate_filesystem(monkeypatch, *paths):
    separate, real = set(paths), destinations._device
    monkeypatch.setattr(
        destinations, "_device", lambda path: -1 if path in separate else real(path)
    )
    return separate


async def test_repick_replaces_staging_saved_by_an_earlier_failed_choice(
    client, admin, database, empty_route, monkeypatch
):
    route = empty_route
    monkeypatch.setattr(library_folders, "Audiobookshelf", route["backend"].client)
    monkeypatch.setattr(get_settings(), "import_destinations", {})
    monkeypatch.setattr(get_settings(), "import_staging_root", None)
    await save_staging(database, "/missing-mount/.book-search-staging")
    body = {
        "library_id": route["destination"]["library_id"],
        "backend_path": "/books",
        "local_path": str(route["target"]),
    }
    separate = separate_filesystem(monkeypatch, route["target"])
    response = await client.put("/api/organization/library-folders/audio", json=body)
    assert response.status_code == 422
    assert "Mount the parent folder instead" in response.json()["detail"]
    assert await saved_staging(database) == "/missing-mount/.book-search-staging"
    separate.clear()
    missing = {**body, "local_path": str(route["target"].parent / "not-mounted")}
    response = await client.put("/api/organization/library-folders/audio", json=missing)
    assert response.status_code == 422
    assert "inside its container" in response.json()["detail"]
    response = await client.put("/api/organization/library-folders/audio", json=body)
    assert response.status_code == 200, response.text
    staging = route["target"].parent / ".book-search-staging"
    assert await saved_staging(database) == str(staging) and staging.is_dir()


async def test_staging_stays_while_unfinished_imports_use_it(
    client, admin, database, destination_route, monkeypatch
):
    route = destination_route
    await start_probe(client, route)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    route["plan"] = (await client.get(f"/api/organization/plans/{route['plan_id']}")).json()
    assert (await start_import(client, route)).status_code == 202
    monkeypatch.setattr(library_folders, "Audiobookshelf", route["backend"].client)
    monkeypatch.setattr(get_settings(), "import_staging_root", None)
    async with database() as db, db.begin():
        (await db.get(Library, UUID(route["library_id"]))).accessible = True
    await save_staging(database, route["stage"])
    separate_filesystem(monkeypatch, route["stage"])

    async def choose():
        current = (await client.get("/api/organization/destinations")).json()[0]
        return await client.put(
            "/api/organization/library-folders/ebook",
            json={
                "library_id": route["library_id"],
                "backend_path": "/books",
                "local_path": str(route["target"]),
                "destination_id": current["id"],
                "expected_revision": current["revision"],
            },
        )

    response = await choose()
    assert response.status_code == 409
    assert str(route["stage"]) in response.json()["detail"]
    assert await saved_staging(database) == str(route["stage"])
    async with database() as db, db.begin():
        for entry in await db.scalars(select(ImportEntry)):
            entry.state = "cancelled"
    receipt = route["stage"] / "earlier-import.json"
    receipt.write_text("{}")
    response = await choose()
    assert response.status_code == 409
    assert "holds records of earlier imports" in response.json()["detail"]
    receipt.unlink()
    response = await choose()
    assert response.status_code == 200, response.text
    assert await saved_staging(database) == str(route["target"].parent / ".book-search-staging")


async def test_library_folder_browser_is_read_only_and_confined(
    client, admin, database, monkeypatch, tmp_path
):
    from app.domain import download_folders

    root = tmp_path.resolve() / "mounted-media"
    library = root / "library"
    library.mkdir(parents=True)
    (library / "existing.epub").write_bytes(b"existing book")
    (root / "outside").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(download_folders, "volume_roots", lambda: {root})
    monkeypatch.setattr(get_settings(), "import_sources", {})
    listed = await client.get("/api/organization/library-folders/browse")
    assert listed.status_code == 200
    assert listed.json()["directories"] == [str(root)]
    folders = await client.get(
        "/api/organization/library-folders/browse", params={"path": str(root)}
    )
    assert folders.json()["directories"] == [str(library)]
    for path in ["/", str(tmp_path), str(root / "outside"), str(library / "existing.epub")]:
        assert (
            await client.get("/api/organization/library-folders/browse", params={"path": path})
        ).status_code == 422
    assert (library / "existing.epub").read_bytes() == b"existing book"
    async with database() as db:
        assert list(await db.scalars(select(ImportDestination))) == []
        assert await db.get(ImportStorageSettings, 1) is None


async def test_library_folder_browser_requires_admin(client):
    assert (await client.get("/api/organization/library-folders/browse")).status_code == 401
