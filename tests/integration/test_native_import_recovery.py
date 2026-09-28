"""Native metadata handoffs remain recoverable without weakening initial verification."""

from pathlib import PurePosixPath
from uuid import UUID
from zipfile import ZipFile

import httpx
import pytest

from app.db.models import ImportDestination, ImportEntry, Integration, Library
from app.importing import destinations, execution
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.contracts.test_grimmory import GrimmoryFixture, book, file
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_destinations import start_probe
from tests.integration.test_import_execution import start

pytestmark = pytest.mark.integration


@pytest.fixture
async def grimmory_route(client, admin, database, destination_route, monkeypatch):  # noqa: F811
    route = destination_route
    fixture = GrimmoryFixture(route["target"])
    fixture.catalog = []
    fixture.persistence["saveToOriginalFile"]["epub"]["enabled"] = True
    monkeypatch.setattr(destinations, "Grimmory", fixture.client)
    monkeypatch.setattr(execution, "Grimmory", fixture.client)
    async with database() as db, db.begin():
        library = await db.get(Library, UUID(route["library_id"]))
        integration = await db.get(Integration, library.integration_id)
        integration.kind = "grimmory"
        integration.encrypted_secrets = encrypt_secrets(
            {"username": "reader", "password": "secret"}
        )
        library.external_id = "7"
        destination = await db.get(ImportDestination, UUID(route["destination"]["id"]))
        destination.mode = "copy"
    route["destination"] = (await client.get("/api/organization/destinations")).json()[0]
    probe = await start_probe(client, route)
    assert probe.status_code == 202, probe.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    route["destination"] = (await client.get("/api/organization/destinations")).json()[0]
    assert route["destination"]["publication_available"], route["destination"]
    route["plan"] = (await client.get(f"/api/organization/plans/{route['plan_id']}")).json()
    route["fixture"] = fixture
    return route


@pytest.mark.parametrize(
    "failure",
    [
        "response",
        "stale-size",
        "before-request",
        "process-crash",
        "wrong-library",
        "renamed",
        "wrong-metadata",
        "unapproved-write",
        "empty-file",
        "missing-file",
    ],
)
async def test_metadata_handoff_recovery(client, admin, database, grimmory_route, failure):
    route = grimmory_route
    fixture = route["fixture"]
    original_handle = fixture.handle
    original_source = (route["source"] / "pack/book.epub").read_bytes()
    if failure == "unapproved-write":
        fixture.persistence["saveToOriginalFile"]["epub"]["enabled"] = False

    def handle(request):
        if request.url.path == "/api/v1/libraries/7/refresh":
            published = next(route["target"].rglob("*.epub"))
            raw = book()
            path = str(PurePosixPath("/books") / published.relative_to(route["target"]))
            raw["primaryFile"] = file(path, size_kb=published.stat().st_size // 1024)
            fixture.catalog = [raw]
        writing = request.method == "PUT" and request.url.path.endswith("/metadata")
        if writing and failure == "before-request":
            raise httpx.ReadTimeout("Request not delivered", request=request)
        response = original_handle(request)
        if writing:
            published = next(route["target"].rglob("*.epub"))
            with ZipFile(published, "a") as epub:
                epub.comment = b"native metadata update" * 100
            if failure != "stale-size":
                fixture.catalog[0]["primaryFile"]["fileSizeKb"] = published.stat().st_size // 1024
            if failure == "process-crash":
                raise RuntimeError("Process stopped after the metadata write")
            raise httpx.ReadTimeout("Lost metadata acknowledgement", request=request)
        return response

    fixture.handle = handle
    response = await start(client, route)
    assert response.status_code == 202, response.text
    entry = response.json()["entries"][0]
    if failure == "process-crash":
        with pytest.raises(RuntimeError, match="Process stopped"):
            await execution.execute(UUID(entry["operation_id"]))
    else:
        await execution.execute(UUID(entry["operation_id"]))
    async with database() as db:
        stored = await db.get(ImportEntry, UUID(entry["id"]))
        assert stored.receipt["grimmory_metadata"]["item_id"] == "1"
    fixture.handle = original_handle
    if failure == "wrong-library":
        fixture.catalog[0]["libraryId"] = 9
    elif failure == "renamed":
        fixture.catalog[0]["primaryFile"]["filePath"] = "/books/elsewhere/book.epub"
    elif failure == "wrong-metadata":
        fixture.catalog[0]["metadata"]["title"] = "A different book"
    elif failure == "empty-file":
        next(route["target"].rglob("*.epub")).write_bytes(b"")
    elif failure == "missing-file":
        next(route["target"].rglob("*.epub")).unlink()
    await execution.execute(UUID(entry["operation_id"]))
    current = (await client.get(f"/api/organization/imports/{response.json()['id']}")).json()[
        "entries"
    ][0]
    if failure in {"response", "stale-size", "before-request", "process-crash"}:
        assert current["state"] == "confirmed", current
        assert current["asset_id"]
    else:
        assert current["state"] == "held", current
        assert current["asset_id"] is None
    # Lost acknowledgement recovery never repeats an already successful metadata PUT.
    assert len(fixture.metadata_updates) == 1
    assert (route["source"] / "pack/book.epub").read_bytes() == original_source
    assert len(list(route["target"].rglob("*.epub"))) == (0 if failure == "missing-file" else 1)


async def test_changed_media_without_metadata_handoff_is_held(client, admin, grimmory_route):
    route = grimmory_route
    result = await start(client, route)
    entry = result.json()["entries"][0]

    def stop(phase):
        if phase == "published-before-database":
            raise RuntimeError("Publication interrupted")

    with pytest.raises(RuntimeError, match="Publication interrupted"):
        await execution.execute(UUID(entry["operation_id"]), checkpoint=stop)
    with ZipFile(next(route["target"].rglob("*.epub")), "a") as epub:
        epub.comment = b"unexplained change"
    await execution.execute(UUID(entry["operation_id"]))
    current = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()[
        "entries"
    ][0]
    assert current["state"] == "held" and current["asset_id"] is None, current
    assert not route["fixture"].metadata_updates
