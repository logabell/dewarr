"""Native review handoff, durable uncertainty and explicit library linking."""

from pathlib import PurePosixPath
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.api import library_folders
from app.db.models import ImportEntry, Integration, Library, LibraryAsset
from app.importing import destinations, execution
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.contracts.test_grimmory import GrimmoryFixture, book
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_destinations import start_probe
from tests.integration.test_import_execution import start

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("shared_default", [True, False])
async def test_bookdrop_activation_preserves_member_defaults(
    client, admin, database, bookdrop_route, shared_default
):
    from app.db.models import AcquisitionDefaults, LibraryGrant, User
    from app.domain.destination_defaults import destination_default
    from app.domain.release_profiles import profile_snapshot

    route = bookdrop_route
    chosen = route["destination"]
    async with database() as db, db.begin():
        member = User(username="member", display_name="Member", role="member", active=True)
        db.add(member)
        await db.flush()
        member_id = member.id
        library_id = UUID(route["library_id"])
        db.add(LibraryGrant(user_id=member.id, library_id=library_id))
        await db.flush()
        direct = await destination_default(db, member, "ebook", library_id, None)
        direct_id = direct.id
        if shared_default:
            db.add(
                AcquisitionDefaults(
                    key="installation",
                    generation=1,
                    preferences={
                        "ebook_library_id": str(library_id),
                        "ebook_destination_id": str(direct_id),
                    },
                )
            )
    activated = await client.post(
        f"/api/organization/library-folders/{chosen['id']}/activate",
        json={"expected_revision": chosen["revision"], "automatic": False},
    )
    assert activated.status_code == 200, activated.text
    async with database() as db:
        member = await db.get(User, member_id)
        profile = await profile_snapshot(db, member.id)
        assert profile.preferences.ebook_destination_id == (direct_id if shared_default else None)
        selected = await destination_default(
            db, member, "ebook", library_id, profile.preferences.ebook_destination_id
        )
        assert selected.id == direct_id
        personal = await profile_snapshot(db, UUID(admin["id"]))
        assert str(personal.preferences.ebook_destination_id) == chosen["id"]
        assert personal.preferences.ebook_library_id is None


@pytest.fixture
async def bookdrop_route(client, admin, database, destination_route, monkeypatch):  # noqa: F811
    route = destination_route
    intake = route["target"].parent / "intake"
    intake.mkdir()
    fixture = GrimmoryFixture(intake)
    fixture.path_root = "/intake"
    fixture.persistence["moveFilesToLibraryPattern"] = True
    fixture.persistence["saveToOriginalFile"]["epub"]["enabled"] = True
    monkeypatch.setattr(destinations, "Grimmory", fixture.client)
    monkeypatch.setattr(execution, "Grimmory", fixture.client)
    monkeypatch.setattr(library_folders, "Grimmory", fixture.client)
    async with database() as db, db.begin():
        library = await db.get(Library, UUID(route["library_id"]))
        integration = await db.get(Integration, library.integration_id)
        integration.kind = "grimmory"
        integration.encrypted_secrets = encrypt_secrets(
            {"username": "reader", "password": "secret"}
        )
        library.external_id = "7"
        integration_id = str(integration.id)
    response = await client.put(
        "/api/organization/library-folders/ebook",
        json={
            "workflow": "bookdrop",
            "integration_id": integration_id,
            "backend_path": "/intake",
            "local_path": str(intake),
            "automatic": False,
        },
    )
    assert response.status_code == 200, response.text
    route.update(destination=response.json(), target=intake, fixture=fixture)
    probe = await start_probe(client, route)
    assert probe.status_code == 202, probe.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    route["destination"] = next(
        row
        for row in (await client.get("/api/organization/destinations")).json()
        if row["workflow"] == "bookdrop"
    )
    assert route["destination"]["publication_available"], route["destination"]
    route["plan"] = (await client.get(f"/api/organization/plans/{route['plan_id']}")).json()
    return route


async def test_bookdrop_handoff_review_and_explicit_link(client, admin, database, bookdrop_route):
    route = bookdrop_route
    result = await start(client, route)
    assert result.status_code == 202, result.text
    run = result.json()
    await get_queue().run_worker_async(wait=False, concurrency=1)
    current = (await client.get(f"/api/organization/imports/{run['id']}")).json()["entries"][0]
    assert current["state"] == "awaiting-review", current
    assert current["asset_id"] is None and not current["can_retry"]
    epub = next(route["target"].rglob("*.epub"))
    source = route["source"] / "pack/book.epub"
    assert epub.name == source.name and epub.read_bytes() == source.read_bytes()
    assert epub.stat().st_ino != source.stat().st_ino
    assert list(epub.parent.iterdir()) == [epub]
    assert route["fixture"].metadata_updates == []
    endpoint = f"/api/organization/imports/{run['id']}/entries/{current['id']}/bookdrop"
    route["fixture"].bookdrop_queue = [
        {
            "id": 88,
            "filePath": str(PurePosixPath("/intake") / epub.relative_to(route["target"])),
            "fileSize": epub.stat().st_size,
        }
    ]
    refreshed = await client.post(endpoint, json={"action": "refresh"})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["entries"][0]["state"] == "awaiting-review"
    route["fixture"].bookdrop_queue = []
    epub.unlink()  # Imported OR discarded: queue absence is deliberately ambiguous.
    refreshed = await client.post(endpoint, json={"action": "refresh"})
    assert refreshed.json()["entries"][0]["state"] == "needs-link"
    duplicate = await start(client, route, "different-command")
    assert duplicate.json()["entries"][0]["state"] == "held"
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(LibraryAsset)) == 0
    candidate = book(1)
    route["fixture"].catalog = [candidate]
    async with database() as db, db.begin():
        asset = LibraryAsset(
            library_id=UUID(route["library_id"]),
            external_id="1",
            version_id=UUID(current["version_id"]),
            medium="ebook",
            state="present",
            full_content=True,
            match_status="matched",
            title="Reviewed title",
            files=[{"path": candidate["primaryFile"]["filePath"]}],
        )
        db.add(asset)
        await db.flush()
        asset_id = str(asset.id)
    linked = await client.post(endpoint, json={"action": "link", "asset_id": asset_id})
    assert linked.status_code == 200, linked.text
    assert linked.json()["entries"][0]["state"] == "confirmed"
    assert linked.json()["entries"][0]["asset_id"] == asset_id
    assert not list(route["target"].rglob("*.epub")) and source.exists()


@pytest.mark.parametrize("phase", ["published-before-receipt", "published-before-database"])
async def test_consumed_handoff_survives_crash_without_resend(
    client, admin, database, bookdrop_route, phase
):
    route = bookdrop_route
    response = await start(client, route)
    run = response.json()
    entry = run["entries"][0]

    def crash(current):
        if current == phase:
            raise RuntimeError("Lost acknowledgement")

    with pytest.raises(RuntimeError, match="Lost acknowledgement"):
        await execution.execute(UUID(entry["operation_id"]), checkpoint=crash)
    next(route["target"].rglob("*.epub")).unlink()
    await execution.execute(UUID(entry["operation_id"]))
    current = (await client.get(f"/api/organization/imports/{run['id']}")).json()["entries"][0]
    assert current["state"] == "awaiting-review", current
    assert current["asset_id"] is None and not list(route["target"].rglob("*.epub"))
    endpoint = f"/api/organization/imports/{run['id']}/entries/{entry['id']}/bookdrop"
    rejected = await client.post(endpoint, json={"action": "reject"})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["entries"][0]["state"] == "rejected"
    async with database() as db:
        assert (await db.get(ImportEntry, UUID(entry["id"]))).reserved
    await execution.execute(UUID(entry["operation_id"]))
    assert not list(route["target"].rglob("*.epub"))


async def test_bookdrop_plans_ignore_library_templates_and_do_not_grant_member_access(
    client, admin, database, bookdrop_route
):
    from app.db.models import OrganizationSettings, User
    from app.domain.destination_defaults import configured_destinations
    from app.importing.naming import NamingProfile

    route = bookdrop_route
    async with database() as db, db.begin():
        settings = await db.get(OrganizationSettings, 1)
        values = NamingProfile(layout="nested", ebook_folder="{publisher}/{title}").model_dump()
        if settings:
            settings.profile = values
        else:
            db.add(OrganizationSettings(id=1, profile=values))
        member = User(
            id=UUID("00000000-0000-0000-0000-000000000099"),
            username="member",
            display_name="Member",
            role="member",
            active=True,
        )
        assert not any(
            row.workflow == "bookdrop" for row in await db.scalars(configured_destinations(member))
        )
    options = await client.get("/api/acquisition/selections/options")
    assert options.status_code == 200, options.text
    option = next(
        row for row in options.json()["destinations"] if row["id"] == route["destination"]["id"]
    )
    assert option["library_id"] is None and "Bookdrop" in option["name"]
    policy = (
        await client.get(
            f"/api/organization/destinations/{route['destination']['id']}/automatic-import"
        )
    ).json()
    assert policy["can_enable"]
    original = route["plan"]
    inspection_id = original["inspection_id"]
    inspection = (await client.get(f"/api/organization/inspections/{inspection_id}")).json()
    settings = (await client.get("/api/organization/settings")).json()
    group = original["document"]["groups"][0]
    response = await client.post(
        f"/api/organization/inspections/{inspection_id}/plans",
        json={
            "inspection_revision": inspection["snapshot"]["revision"],
            "profile_revision": settings["revision"],
            "destinations": {"ebook": route["destination"]["id"]},
            "selections": [
                {
                    "group_key": inspection["snapshot"]["groups"][0]["key"],
                    "work_id": group["work_id"],
                    "version_id": group["version_id"],
                    "full_content": True,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    route["plan"], route["plan_id"] = response.json(), response.json()["id"]
    assert route["plan"]["document"]["bookdrop_media"] == ["ebook"]
    planned = route["plan"]["document"]["plan"]["items"][0]
    assert planned["state"] == "ready" and planned["files"][0]["destination"].endswith("/book.epub")
    result = await start(client, route)
    assert result.status_code == 202, result.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    current = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()[
        "entries"
    ][0]
    assert current["state"] == "awaiting-review", current
