# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.api import integrations, library_folders
from app.db.models import ImportDestination, Integration, Library, Operation
from app.domain.inventory import synchronize
from app.importing.destinations import destination_configuration
from tests.contracts.test_audiobookshelf import ABSFixture, connect, sync
from tests.integration.test_setup_probe import empty_route  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("changes", "invalidated"),
    [
        ({"name": "Renamed library server"}, False),
        ({"public_url": "https://books.example.test"}, False),
        ({"token": "private-import-token"}, False),
        ({"token": "replacement-token"}, True),
        ({"base_url": "http://replacement.test"}, True),
        ({"enabled": False}, True),
    ],
)
async def test_connection_edit_only_invalidates_library_access_when_needed(
    client, admin, database, empty_route, monkeypatch, changes, invalidated
):
    monkeypatch.setattr(library_folders, "Audiobookshelf", empty_route["backend"].client)

    async def inspect(*args):
        return {"operations": ["scan"], "library_count": 1, "book_count": 0}

    monkeypatch.setattr(integrations, "inspect_connection", inspect)
    async with database() as db, db.begin():
        library = await db.get(Library, UUID(empty_route["destination"]["library_id"]))
        connection = await db.get(Integration, library.integration_id)
        identifier, generation = connection.id, connection.credential_generation
        lease = uuid4()
        connection.lease_token = lease
        connection.lease_until = datetime.now(UTC) + timedelta(minutes=2)
        destination = await db.get(ImportDestination, UUID(empty_route["destination"]["id"]))
        before = await destination_configuration(db, destination)
    assert len((await client.get("/api/organization/library-folders")).json()) == 1
    response = await client.put(
        f"/api/integrations/{identifier}",
        json={"name": "ABS", "base_url": "http://fixture", **changes},
    )
    assert response.status_code == 200, response.text
    options = (await client.get("/api/organization/library-folders")).json()
    assert bool(options) is not invalidated
    async with database() as db:
        connection = await db.get(Integration, identifier)
        library = await db.get(Library, library.id)
        assert library.accessible is not invalidated
        assert connection.credential_generation == generation + int(invalidated)
        assert connection.lease_token == (None if invalidated else lease)
        destination = await db.get(ImportDestination, destination.id)
        after = await destination_configuration(db, destination)
        assert (before != after) is invalidated


async def test_sync_restores_hidden_folders_without_reconnecting(
    client, admin, database, monkeypatch
):
    class FolderABS(ABSFixture):
        async def handle(self, request):
            if request.url.path == "/abs/api/libraries/library-one":
                return httpx.Response(
                    200,
                    json={
                        "id": "library-one",
                        "mediaType": "book",
                        "folders": [{"fullPath": "/audiobooks"}],
                        "settings": {},
                    },
                )
            return await super().handle(request)

    fixture = FolderABS({})
    monkeypatch.setattr(library_folders, "Audiobookshelf", fixture.client)
    connection = await connect(client)
    assert (await client.get("/api/organization/library-folders")).json() == []
    await sync(client, connection, fixture, "initial-folder-sync")
    options = (await client.get("/api/organization/library-folders")).json()
    assert options[0]["folders"] == ["/audiobooks"]
    # Reproduce an installation whose libraries were hidden by the old edit path.
    async with database() as db, db.begin():
        library = await db.scalar(select(Library))
        library.accessible = False
    assert (await client.get("/api/organization/library-folders")).json() == []
    await sync(client, connection, fixture, "restore-folder-sync")
    assert (await client.get("/api/organization/library-folders")).json() == options


async def test_shared_sync_status_is_exact_and_admin_only(client, admin, database):
    connection = await connect(client)
    first = await client.post(
        f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": "first-admin-sync"}
    )
    assert first.status_code == 202, first.text
    operation_id = UUID(first.json()["id"])
    endpoint = f"/api/integrations/{connection}/sync/{operation_id}"
    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as second:
        assert (await second.get(endpoint)).status_code == 401
        for role in ("admin", "member"):
            created = await client.post(
                "/api/auth/users",
                json={
                    "username": f"second-{role}",
                    "display_name": f"Second {role}",
                    "role": role,
                    "password": "a long second account password",
                },
            )
            assert created.status_code == 201, created.text
            second.cookies.clear()
            login = await second.post(
                "/api/auth/login",
                json={
                    "username": f"second-{role}",
                    "password": "a long second account password",
                },
            )
            assert login.status_code == 200, login.text
            second.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            if role == "member":
                assert (await second.get(endpoint)).status_code == 403
                continue
            coalesced = await second.post(
                f"/api/integrations/{connection}/sync",
                headers={"Idempotency-Key": "second-admin-sync"},
            )
            assert coalesced.status_code == 202, coalesced.text
            assert coalesced.json()["id"] == str(operation_id)
            assert not any(
                row["id"] == str(operation_id) for row in (await second.get("/api/activity")).json()
            )
            status = await second.get(endpoint)
            assert status.status_code == 200, status.text
            assert status.json()["status"] == "queued"
            await synchronize(operation_id, client_factory=ABSFixture({}).client)
            assert (await second.get(endpoint)).json()["status"] == "completed"

    # Exact lookup remains usable beyond the current owner's latest 100 entries.
    async with database() as db, db.begin():
        db.add_all(
            Operation(owner_id=UUID(admin["id"]), kind="system.probe", idempotency_key=f"noise-{i}")
            for i in range(101)
        )
        unrelated = Operation(
            owner_id=UUID(admin["id"]),
            integration_id=UUID(connection),
            kind="system.probe",
            idempotency_key="unrelated-kind",
        )
        db.add(unrelated)
        await db.flush()
        unrelated_id = unrelated.id
    assert not any(
        row["id"] == str(operation_id) for row in (await client.get("/api/activity")).json()
    )
    assert (await client.get(endpoint)).json()["status"] == "completed"
    other_connection = await connect(client)
    for path in (
        f"/api/integrations/{other_connection}/sync/{operation_id}",
        f"/api/integrations/{connection}/sync/{unrelated_id}",
        f"/api/integrations/{connection}/sync/{uuid4()}",
    ):
        assert (await client.get(path)).status_code == 404
