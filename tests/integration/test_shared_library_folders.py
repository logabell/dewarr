# ruff: noqa: F811
"""A mixed NAS library through setup, route verification, publication and confirmation."""

from uuid import UUID

import pytest
from sqlalchemy import select

from app.api import library_folders
from app.config import get_settings
from app.db.models import (
    ImportDestination,
    ImportStorageSettings,
    Integration,
    LibraryAsset,
    Version,
    Work,
)
from app.importing import destinations, execution
from app.jobs.queue import get_queue
from tests.abs_import_fixture import ScanningBackend
from tests.filesystem_fixtures import read_only_downloads  # noqa: F401
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_execution import ready_route  # noqa: F401
from tests.integration.test_import_execution import start as start_import
from tests.integration.test_import_inspections import submit
from tests.integration.test_setup_probe import empty_route, start  # noqa: F401
from tests.media_fixtures import audio, epub

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("folder_name,readonly", [("Reading Room", False), ("shelf-42", True)])
async def test_mixed_mount_first_save_and_both_formats_import(
    client, admin, database, empty_route, monkeypatch, read_only_downloads, folder_name, readonly
):
    route = empty_route
    target = route["target"].with_name(folder_name)
    route["target"].rename(target)
    backend = ScanningBackend(target)
    backend.backend_path = "/remote-collection"
    for module in (library_folders, destinations, execution):
        monkeypatch.setattr(module, "Audiobookshelf", backend.client)
    monkeypatch.setattr(destinations, "filesystem_mounts", lambda: [(target, "ext4", set())])
    monkeypatch.setattr(get_settings(), "import_destinations", {})
    monkeypatch.setattr(get_settings(), "import_staging_root", None)
    async with database() as db, db.begin():
        await db.delete(await db.get(ImportDestination, UUID(route["destination"]["id"])))
        downloader = await db.get(Integration, route["downloader"])
        downloader.config = {**downloader.config, "save_path": "/downloads"}
        work = Work(title="First Harbor", authors=["Alex Morgan"])
        db.add(work)
        await db.flush()
        versions = {
            medium: Version(work_id=work.id, medium=medium) for medium in ("ebook", "audio")
        }
        db.add_all(versions.values())
        await db.flush()
        work_id = str(work.id)
        version_ids = {medium: str(version.id) for medium, version in versions.items()}
    # Both versions deliberately lack edition year and narrator: old templates collide.
    epub(route["source"] / "pack/book.epub")
    audio(route["source"] / "pack/book.m4b", narrator="")
    originals = {
        p.name: (p.read_bytes(), p.stat().st_ino) for p in (route["source"] / "pack").iterdir()
    }
    if readonly:
        read_only_downloads(route["source"])
    chosen = {}
    for medium in ("ebook", "audio"):
        response = await client.put(
            f"/api/organization/library-folders/{medium}",
            json={
                "library_id": route["destination"]["library_id"],
                "backend_path": backend.backend_path,
                "local_path": str(target),
            },
        )
        assert response.status_code == 200, response.text
        route["destination"] = response.json()
        probe = await start(client, route, key=f"shared-{medium}")
        assert probe.status_code == 202, probe.text
        await get_queue().run_worker_async(wait=False, concurrency=1)
        saved = (await client.get("/api/organization/destinations")).json()
        chosen = {row["medium"]: row for row in saved}
        assert all(row["publication_available"] for row in saved), saved
    assert all(row["shared_root"] for row in chosen.values())
    assert {row["staging_path"] for row in chosen.values()} == {
        str(target / ".book-search-staging")
    }
    assert {row["mode"] for row in chosen.values()} == {"copy" if readonly else "hardlink"}
    response = await submit(client)
    assert response.status_code == 202, response.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    record = (await client.get(f"/api/organization/inspections/{response.json()['id']}")).json()
    profile = (await client.get("/api/organization/settings")).json()
    response = await client.post(
        f"/api/organization/inspections/{record['id']}/plans",
        json={
            "inspection_revision": record["snapshot"]["revision"],
            "profile_revision": profile["revision"],
            "selections": [
                {
                    "group_key": group["key"],
                    "work_id": work_id,
                    "version_id": version_ids[group["medium"]],
                    "full_content": True,
                }
                for group in record["snapshot"]["groups"]
            ],
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    response = await client.post(
        f"/api/organization/plans/{plan['id']}/imports",
        headers={"Idempotency-Key": "mixed-import"},
        json={
            "plan_revision": plan["revision"],
            "destinations": {
                medium: {"id": row["id"], "revision": row["revision"]}
                for medium, row in chosen.items()
            },
        },
    )
    assert response.status_code == 202, response.text
    run = response.json()
    await get_queue().run_worker_async(wait=False, concurrency=1)
    updated = (await client.get(f"/api/organization/imports/{run['id']}")).json()
    assert len(updated["entries"]) == 2
    assert all(entry["state"] == "confirmed" for entry in updated["entries"]), updated
    for extension, label in [("epub", "Ebook"), ("m4b", "Audiobook")]:
        folder = target / "Alex Morgan" / f"First Harbor ({label})"
        paths = list(folder.glob(f"*.{extension}"))
        assert len(paths) == 1
        path = paths[0]
        content, inode = originals[f"book.{extension}"]
        assert path.read_bytes() == content
        assert (path.stat().st_ino == inode) is (not readonly)
        assert (route["source"] / "pack" / f"book.{extension}").read_bytes() == content
    async with database() as db:
        assets = list(await db.scalars(select(LibraryAsset)))
        assert {str(asset.version_id) for asset in assets} == set(version_ids.values())
        assert all(asset.full_content for asset in assets)


async def test_picker_names_a_hidden_configured_overlap(
    client, admin, empty_route, monkeypatch, caplog
):
    route = empty_route
    hidden_source = route["target"] / "earlier-download-location"
    monkeypatch.setattr(library_folders, "Audiobookshelf", route["backend"].client)
    monkeypatch.setattr(get_settings(), "import_staging_root", None)
    monkeypatch.setattr(get_settings(), "import_sources", {"old-mapping": hidden_source})
    response = await client.put(
        "/api/organization/library-folders/audio",
        json={
            "library_id": route["destination"]["library_id"],
            "backend_path": "/books",
            "local_path": str(route["target"]),
        },
    )
    assert response.status_code == 422
    message = response.json()["detail"]
    assert str(hidden_source) in message and str(route["target"]) in message
    assert "old-mapping" in message and "BOOK_IMPORT_SOURCES" in message
    assert message in caplog.text
    assert list(route["target"].iterdir()) == []


async def test_shared_library_does_not_change_other_library_plans(
    client, admin, database, ready_route, monkeypatch
):
    route = ready_route
    settings = get_settings()
    shared = route["target"].with_name("mixed-collection")
    shared.mkdir()
    monkeypatch.setattr(
        settings,
        "import_destinations",
        {
            **settings.import_destinations,
            "mixed-ebook": shared,
            "mixed-audio": shared,
        },
    )
    async with database() as db, db.begin():
        mixed = {
            medium: ImportDestination(
                root_key=f"mixed-{medium}",
                library_id=UUID(route["library_id"]),
                medium=medium,
                backend_path="/mixed",
            )
            for medium in ("ebook", "audio")
        }
        db.add_all(mixed.values())
        await db.flush()
        mixed_id = str(mixed["ebook"].id)
    # An unrelated library becoming mixed must not hold an already reviewed plan.
    response = await start_import(client, route)
    assert response.status_code == 202, response.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    updated = (await client.get(f"/api/organization/imports/{response.json()['id']}")).json()
    assert updated["entries"][0]["state"] == "confirmed", updated
    assert (route["target"] / "Alex Morgan/First Harbor/First Harbor.epub").exists()

    # New plans and previews also take their naming from their chosen destination.
    old = route["plan"]["document"]
    record = (
        await client.get(f"/api/organization/inspections/{route['plan']['inspection_id']}")
    ).json()
    profile = (await client.get("/api/organization/settings")).json()
    body = {
        "inspection_revision": record["snapshot"]["revision"],
        "profile_revision": profile["revision"],
        "selections": [
            {
                "group_key": record["snapshot"]["groups"][0]["key"],
                "work_id": old["groups"][0]["work_id"],
                "version_id": old["groups"][0]["version_id"],
                "full_content": True,
            }
        ],
    }
    endpoint = f"/api/organization/inspections/{record['id']}/plans"
    assert (await client.post(endpoint, json=body)).status_code == 422  # Ambiguous destination.
    for identifier, expected in [(route["destination"]["id"], []), (mixed_id, ["ebook"])]:
        selected = {"ebook": identifier}
        response = await client.post(endpoint, json={**body, "destinations": selected})
        assert response.status_code == 201, response.text
        plan = response.json()
        document = plan["document"]
        assert document["shared_media"] == expected
        assert document["plan"]["items"][0]["folder"].endswith(" (Ebook)") is bool(expected)
        preview = await client.post(
            "/api/organization/preview",
            json={
                "groups": old["groups"],
                "destinations": selected,
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["items"][0]["folder"] == document["plan"]["items"][0]["folder"]
        other_id = route["destination"]["id"] if expected else mixed_id
        rejected = await client.post(
            f"/api/organization/plans/{plan['id']}/imports",
            headers={"Idempotency-Key": f"changed-naming-{identifier}"},
            json={
                "plan_revision": plan["revision"],
                "destinations": {
                    "ebook": {"id": other_id, "revision": route["destination"]["revision"]}
                },
            },
        )
        assert rejected.status_code == 409, rejected.text
        assert "different folder names" in rejected.json()["detail"]


async def test_repick_keeps_legacy_journals_even_when_another_route_uses_protected_storage(
    client, admin, database, empty_route, monkeypatch
):
    route = empty_route
    monkeypatch.setattr(library_folders, "Audiobookshelf", route["backend"].client)
    monkeypatch.setattr(
        library_folders,
        "storage_locations",
        lambda settings: [
            (route["staging"], settings.import_journal_root),
            (route["staging"], None),
        ],
    )
    current = route["destination"]
    response = await client.put(
        "/api/organization/library-folders/ebook",
        json={
            "library_id": current["library_id"],
            "backend_path": "/books",
            "local_path": str(route["target"]),
            "destination_id": current["id"],
            "expected_revision": current["revision"],
        },
    )
    assert response.status_code == 200, response.text
    async with database() as db:
        storage = await db.get(ImportStorageSettings, 1)
        assert storage.storage_routes[current["root_key"]]["journal_root"] is None
