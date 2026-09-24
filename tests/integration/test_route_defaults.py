# ruff: noqa: F401, F811
"""Default routes choose destinations without granting or silently renewing authority."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.models import (
    AutomaticImportPolicy,
    DownloadAttempt,
    ImportDestination,
    Integration,
    Library,
    ListAcquisitionBook,
    ListAcquisitionPolicy,
    Operation,
    User,
)
from app.domain import series_acquisition
from tests.integration.test_acquisition import catalog
from tests.integration.test_acquisition_defaults import save
from tests.integration.test_acquisition_selections import selection_route
from tests.integration.test_automatic_dispatch import authorized
from tests.integration.test_automatic_pack_selection import series_pack
from tests.integration.test_automatic_selection import source
from tests.integration.test_list_policies import activate, add, policy_fixture, preview, tick
from tests.integration.test_series_acquisition import BASE, accept
from tests.integration.test_series_acquisition import ready as series_ready

pytestmark = pytest.mark.integration


async def route_defaults(client, route):
    await save(client, {"downloader_id": route["downloader_id"]}, "installation")
    await save(client, {"audio_destination_id": route["destination_id"]})


async def test_list_defaults_resolve_once_and_changed_defaults_hold_existing_automation(
    client, database, policy_fixture
):
    await route_defaults(client, policy_fixture["source"]["body"])
    plan = await preview(
        client, policy_fixture, downloader_id=None, downloader_generation=None, routes={}
    )
    config = plan["configuration"]
    assert config["route_options"] == {
        "downloader_id": None,
        "downloader_generation": None,
        "routes": {},
    }
    assert config["profile"]["scope_origins"]["downloader_id"] == "Installation default"
    assert config["profile"]["scope_origins"]["audio_destination_id"] == "Personal default"
    assert config["routes"]["audio"] == policy_fixture["config"]["routes"]["audio"]
    policy = await activate(client, policy_fixture, plan)
    reopened = (await client.get(f"/api/lists/{policy_fixture['list']}/acquisition")).json()
    assert reopened["configuration"]["route_options"] == config["route_options"]
    await add(client, policy_fixture)
    await tick(database, policy)
    async with database() as db:
        book = await db.scalar(select(ListAcquisitionBook))
        assert book.state == "searching", book.message
    await save(client, {"audio_destination_id": None})
    await tick(database, policy, force_books=True)
    async with database() as db:
        saved = await db.get(ListAcquisitionPolicy, UUID(policy["id"]))
        assert "preferences changed" in saved.message.lower(), saved.message
        assert saved.configuration["routes"] == config["routes"]
        assert not await db.scalar(select(DownloadAttempt.id))
    assert config["routes"]["audio"] == policy_fixture["config"]["routes"]["audio"]


async def test_series_uses_inherited_routes_and_retains_the_reviewed_configuration(
    client, database, series_ready, authorized
):
    await route_defaults(client, authorized["body"])
    response = await client.post(
        BASE + "/preview",
        json={**series_ready["command"], "automatic": {}},
        headers={"Idempotency-Key": "inherited-series-route-preview"},
    )
    assert response.status_code == 201, response.text
    ready = {**series_ready, "parent": UUID(response.json()["id"])}
    identifier = await accept(client, database, ready)
    await series_acquisition.run(identifier)
    async with database() as db:
        row = await db.get(Operation, identifier)
        assert {book["state"] for book in row.payload["books"].values()} == {"searching"}, (
            row.message
        )
        config = row.payload["configuration"]
        assert config["downloader_id"] == authorized["body"]["downloader_id"]
        assert config["profile"]["scope_origins"]["audio_destination_id"] == "Personal default"


async def test_default_route_preview_does_not_renew_a_changed_import_approval(
    client, database, policy_fixture
):
    await route_defaults(client, policy_fixture["source"]["body"])
    plan = await preview(
        client, policy_fixture, downloader_id=None, downloader_generation=None, routes={}
    )
    async with database() as db, db.begin():
        (await db.scalar(select(AutomaticImportPolicy))).generation += 1
    response = await client.post(
        f"/api/lists/{policy_fixture['list']}/acquisition/previews/{plan['id']}/activate"
    )
    assert response.status_code == 409, response.text


async def test_unavailable_default_is_not_replaced_but_an_explicit_route_can_override_it(
    client, policy_fixture
):
    await save(client, {"audio_destination_id": str(uuid4())})
    response = await client.post(
        f"/api/lists/{policy_fixture['list']}/acquisition/preview",
        json={**policy_fixture["config"], "routes": {}},
        headers={"Idempotency-Key": "unavailable-route-default"},
    )
    assert response.status_code == 409 and "unavailable" in response.text
    plan = await preview(client, policy_fixture)
    assert plan["configuration"]["routes"] == policy_fixture["config"]["routes"]


async def test_default_destination_does_not_bypass_library_access(
    client, database, admin, policy_fixture
):
    await route_defaults(client, policy_fixture["source"]["body"])
    async with database() as db, db.begin():
        user = await db.get(User, UUID(admin["id"]))
        user.role, user.can_automate = "member", True
    response = await client.post(
        f"/api/lists/{policy_fixture['list']}/acquisition/preview",
        json={**policy_fixture["config"], "routes": {}},
        headers={"Idempotency-Key": "private-route-default"},
    )
    assert response.status_code == 409, response.text
    assert "Settings → Libraries" in response.json()["detail"]


async def test_clearing_destination_override_uses_library_folder_without_restoring_inheritance(
    client, policy_fixture
):
    route = policy_fixture["source"]["body"]
    await save(client, {"audio_destination_id": route["destination_id"]}, "installation")
    cleared = await save(client, {"audio_destination_id": None})
    assert cleared["overrides"] == {"audio_destination_id": None}
    assert cleared["effective"].get("audio_destination_id") is None
    response = await client.post(
        f"/api/lists/{policy_fixture['list']}/acquisition/preview",
        json={**policy_fixture["config"], "routes": {}},
        headers={"Idempotency-Key": "cleared-route-default"},
    )
    assert response.status_code == 201, response.text
    assert (
        response.json()["configuration"]["routes"]["audio"]["destination_id"]
        == route["destination_id"]
    )
    assert (
        response.json()["configuration"]["profile"]["scope_origins"]["audio_destination_id"]
        == "Configured library folder"
    )
    restored = await save(client, {})
    assert restored["effective"]["audio_destination_id"] == route["destination_id"]


async def test_saved_usenet_client_backs_up_a_torrent_default(client, database, policy_fixture):
    route = policy_fixture["source"]["body"]
    async with database() as db, db.begin():
        primary = await db.get(Integration, UUID(route["downloader_id"]))
        usenet = Integration(
            kind="sabnzbd",
            name="Fixture Usenet",
            base_url="http://sab.test",
            encrypted_secrets=primary.encrypted_secrets,
            credential_generation=primary.credential_generation,
            status="connected",
            config=deepcopy(primary.config),
        )
        db.add(usenet)
        await db.flush()
        usenet_id = str(usenet.id)
    await save(
        client,
        {"downloader_id": route["downloader_id"], "usenet_downloader_id": usenet_id},
        "installation",
    )
    await save(client, {"audio_destination_id": route["destination_id"]})
    plan = await preview(
        client, policy_fixture, downloader_id=None, downloader_generation=None, routes={}
    )
    config = plan["configuration"]
    assert config["route_options"] == {
        "downloader_id": None,
        "downloader_generation": None,
        "routes": {},
    }
    assert config["downloader_id"] == route["downloader_id"]
    assert config["alternate_downloader_id"] == usenet_id
    assert config["alternate_routes"]["audio"] == config["routes"]["audio"]


async def test_only_client_is_used_without_saving_a_downloader_default(client, policy_fixture):
    route = policy_fixture["source"]["body"]
    await save(client, {"audio_destination_id": route["destination_id"]})
    plan = await preview(
        client, policy_fixture, downloader_id=None, downloader_generation=None, routes={}
    )
    assert plan["configuration"]["downloader_id"] == route["downloader_id"]


async def test_sole_usenet_client_is_automatically_available_alongside_torrent(
    client, database, policy_fixture
):
    route = policy_fixture["source"]["body"]
    async with database() as db, db.begin():
        primary = await db.get(Integration, UUID(route["downloader_id"]))
        usenet = Integration(
            kind="sabnzbd",
            name="Only Usenet client",
            base_url="http://sab.test",
            encrypted_secrets=primary.encrypted_secrets,
            credential_generation=primary.credential_generation,
            status="connected",
            config=deepcopy(primary.config),
        )
        disabled = Integration(
            kind="nzbget",
            name="Disabled Usenet client",
            base_url="http://disabled.test",
            enabled=False,
            encrypted_secrets=primary.encrypted_secrets,
            status="connected",
            config=deepcopy(primary.config),
        )
        db.add_all([usenet, disabled])
        await db.flush()
        usenet_id = str(usenet.id)
    await save(client, {"audio_destination_id": route["destination_id"]})
    plan = await preview(
        client, policy_fixture, downloader_id=None, downloader_generation=None, routes={}
    )
    assert plan["configuration"]["downloader_id"] == route["downloader_id"]
    assert plan["configuration"]["alternate_downloader_id"] == usenet_id


async def test_multiple_torrent_clients_require_an_explicit_default(
    client, database, policy_fixture
):
    route = policy_fixture["source"]["body"]
    async with database() as db, db.begin():
        primary = await db.get(Integration, UUID(route["downloader_id"]))
        second = Integration(
            kind="transmission",
            name="Second torrent client",
            base_url="http://transmission.test",
            encrypted_secrets=primary.encrypted_secrets,
            credential_generation=primary.credential_generation,
            status="connected",
            config=deepcopy(primary.config),
        )
        db.add(second)
    await save(client, {"audio_destination_id": route["destination_id"]})
    response = await client.post(
        f"/api/lists/{policy_fixture['list']}/acquisition/preview",
        json={
            **policy_fixture["config"],
            "downloader_id": None,
            "downloader_generation": None,
            "routes": {},
        },
        headers={"Idempotency-Key": "ambiguous-torrent-default"},
    )
    assert response.status_code == 422, response.text
    assert "default downloader" in response.json()["detail"]
    await save(
        client,
        {
            "torrent_downloader_id": route["downloader_id"],
            "audio_destination_id": route["destination_id"],
        },
    )
    plan = await preview(
        client, policy_fixture, downloader_id=None, downloader_generation=None, routes={}
    )
    assert plan["configuration"]["downloader_id"] == route["downloader_id"]


async def test_library_folder_is_used_without_saving_any_route_defaults(client, policy_fixture):
    plan = await preview(
        client, policy_fixture, downloader_id=None, downloader_generation=None, routes={}
    )
    assert plan["configuration"]["routes"] == policy_fixture["config"]["routes"]
    assert (
        plan["configuration"]["profile"]["scope_origins"]["audio_destination_id"]
        == "Configured library folder"
    )


@pytest.mark.parametrize(
    "extra_state", ["enabled", "disabled", "deleted", "ebook", "other-library"]
)
async def test_destination_inference_respects_library_medium_and_configured_choices(
    client, database, catalog, policy_fixture, extra_state
):
    from datetime import UTC, datetime

    async with database() as db, db.begin():
        original = await db.get(
            ImportDestination, UUID(policy_fixture["source"]["body"]["destination_id"])
        )
        library_id = original.library_id
        if extra_state == "other-library":
            original_library = await db.get(Library, original.library_id)
            library = Library(
                integration_id=original_library.integration_id,
                external_id="second",
                name="Other library",
            )
            db.add(library)
            await db.flush()
            library_id = library.id
        extra = ImportDestination(
            root_key="second-folder",
            library_id=library_id,
            medium="ebook" if extra_state == "ebook" else "audio",
            backend_path="/other",
            enabled=extra_state != "disabled",
            deleted_at=datetime.now(UTC) if extra_state == "deleted" else None,
        )
        db.add(extra)
        await db.flush()
        extra_id = str(extra.id)
    if extra_state == "other-library":
        # The requested library takes precedence over a default for another library.
        await save(client, {"audio_destination_id": extra_id})
    response = await client.post(
        f"/api/lists/{policy_fixture['list']}/acquisition/preview",
        json={
            **policy_fixture["config"],
            "routes": {},
            "specification": {"mode": "audio", "audio_library_id": str(catalog["library"])},
        },
        headers={"Idempotency-Key": "inferred-library-destination"},
    )
    if extra_state == "enabled":
        # Even an unverified second destination is a choice, not permission to switch folders.
        assert response.status_code == 422, response.text
        assert "Several audiobook library folders" in response.text
        await save(
            client, {"audio_destination_id": policy_fixture["source"]["body"]["destination_id"]}
        )
        plan = await preview(client, policy_fixture, routes={})
        assert plan["configuration"]["routes"] == policy_fixture["config"]["routes"]
    else:
        assert response.status_code == 201, response.text
        assert response.json()["configuration"]["routes"] == policy_fixture["config"]["routes"]
    if extra_state in {"disabled", "deleted"}:
        options = (await client.get("/api/acquisition/selections/options")).json()
        assert len(options["destinations"]) == 1


@pytest.mark.parametrize("state", ["unverified", "no-approval", "missing", "private"])
async def test_inferred_destination_still_requires_valid_setup_and_access(
    client, database, admin, policy_fixture, state
):
    async with database() as db, db.begin():
        destination = await db.get(
            ImportDestination, UUID(policy_fixture["source"]["body"]["destination_id"])
        )
        if state == "unverified":
            destination.probe = None
        elif state == "no-approval":
            (await db.scalar(select(AutomaticImportPolicy))).enabled = False
        elif state == "missing":
            destination.enabled = False
        else:
            user = await db.get(User, UUID(admin["id"]))
            user.role, user.can_automate = "member", True
    response = await client.post(
        f"/api/lists/{policy_fixture['list']}/acquisition/preview",
        json={**policy_fixture["config"], "routes": {}},
        headers={"Idempotency-Key": "inferred-folder-still-needs-setup"},
    )
    assert response.status_code in {409, 422}, response.text
    if state in {"missing", "private"}:
        assert "Settings → Libraries" in response.json()["detail"]
    async with database() as db:
        assert not await db.scalar(select(DownloadAttempt.id))


async def test_fallback_client_cannot_choose_a_different_library_folder(
    client, database, admin, policy_fixture, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.domain import automatic_routes

    async with database() as db, db.begin():
        original = await db.get(
            ImportDestination, UUID(policy_fixture["source"]["body"]["destination_id"])
        )
        other = ImportDestination(
            root_key="other-final-folder",
            library_id=original.library_id,
            medium="audio",
            backend_path="/other",
        )
        db.add(other)
        await db.flush()
        # Only the other folder would work for the alternate client's mapping.
        verify = AsyncMock(side_effect=lambda _db, dest, _config, _mapping: dest.id == other.id)
        monkeypatch.setattr(automatic_routes, "verified_probe", verify)
        user = await db.get(User, UUID(admin["id"]))
        route = await automatic_routes.verified_destination(
            db, user, "audio", {"source_key": "other"}, original.id
        )
        assert route is None
        assert verify.await_count == 1
        assert verify.call_args.args[1].id == original.id
