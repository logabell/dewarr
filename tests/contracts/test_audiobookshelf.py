import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update

from app.adapters.audiobookshelf import Audiobookshelf, parse_item, readable_item
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.http import configured_url
from app.db.models import Integration, LibraryAsset, Operation, Version, Work
from app.domain.inventory import synchronize


def book(identifier="book-one", *, audio=True, ebook=None, narrator="Jordan Lee"):
    files = []
    media = {
        "metadata": {
            "title": "The First Harbor",
            "authors": [{"name": "Alex Morgan"}],
            "narrators": [narrator],
            "language": "en",
            "publishedYear": "2024",
        },
        "audioFiles": [],
    }
    for ext in (["m4b"] if audio else []) + ([ebook] if ebook else []):
        file = {
            "ino": f"inode-{identifier}-{ext}",
            "metadata": {
                "path": f"/private/library/{identifier}/book.{ext}",
                "ext": f".{ext}",
                "size": 1000,
                "mtimeMs": 12345,
            },
        }
        files.append(copy.deepcopy(file))
        if ext == "m4b":
            media["audioFiles"].append({**file, "duration": 3600})
        else:
            media["ebookFile"] = {**file, "ebookFormat": ext}
    return {
        "id": identifier,
        "libraryId": "library-one",
        "mediaType": "book",
        "media": media,
        "libraryFiles": files,
        "updatedAt": 1,
        "isMissing": False,
        "isInvalid": False,
    }


class ABSFixture:
    """Synthetic HTTP contract, not live ABS scanner certification."""

    def __init__(self, items):
        self.items = items
        self.calls = []
        self.fail_batch = False
        self.scope = []
        self.hide_libraries = False
        self.on_page = None

    async def handle(self, request):
        assert request.headers["authorization"] == "Bearer private-abs-token"
        self.calls.append(request.url.path)
        path = request.url.path.removeprefix("/abs/")
        if path == "api/authorize":
            return httpx.Response(
                200,
                json={
                    "user": {
                        "id": "fixture-user",
                        "type": "user",
                        "permissions": {"librariesAccessible": self.scope},
                    },
                    "serverVersion": "2.36.1",
                },
            )
        if path == "api/libraries":
            return httpx.Response(
                200,
                json={
                    "libraries": []
                    if self.hide_libraries
                    else [{"id": "library-one", "name": "Private audio", "mediaType": "book"}]
                },
            )
        if path == "api/libraries/library-one/items":
            if self.on_page:
                await self.on_page()
            values = list(self.items.values())
            start = int(request.url.params["page"]) * 100
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": b["id"],
                            "updatedAt": b["updatedAt"],
                            "isMissing": b["isMissing"],
                            "isInvalid": b["isInvalid"],
                        }
                        for b in values[start : start + 100]
                    ],
                    "total": len(values),
                },
            )
        if path == "api/items/batch/get":
            if self.fail_batch:
                return httpx.Response(500, text="private upstream diagnostic")
            ids = json.loads(request.content)["libraryItemIds"]
            return httpx.Response(200, json={"libraryItems": [self.items[i] for i in ids]})
        if path.startswith("api/items/"):
            item = self.items.get(path.removeprefix("api/items/"))
            return httpx.Response(200, json=item) if item else httpx.Response(404)
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    def client(self, endpoint, token):
        return Audiobookshelf(endpoint, token, transport=httpx.MockTransport(self.handle))


async def connect(client):
    from unittest.mock import patch

    with patch("app.api.integrations.Audiobookshelf", ABSFixture({}).client):
        response = await client.post(
            "/api/integrations",
            json={
                "name": "Home ABS",
                "base_url": "http://abs.test/abs",
                "public_url": "https://books.test/abs",
                "token": "private-abs-token",
            },
        )
    assert response.status_code == 201, response.text
    assert "private-abs-token" not in response.text
    return response.json()["id"]


async def sync(client, connection, fixture, key):
    response = await client.post(
        f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": key}
    )
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    await synchronize(identifier, client_factory=fixture.client)
    return identifier


def test_media_evidence_and_invalid_payloads():
    assert parse_item(book(ebook="epub")).full_ebook
    assert not parse_item(book(ebook="pdf")).full_ebook
    assert parse_item(book(ebook="pdf")).ebook_supplementary
    assert parse_item(book(audio=False, ebook="pdf")).full_ebook
    broken = book()
    broken["libraryFiles"] = []
    with pytest.raises(AdapterError) as error:
        parse_item(broken)
    assert error.value.kind == FailureKind.PARSER
    for invalid in [
        "http://user:secret@localhost",
        "http://localhost/%2e%2e/secret",
        "file:///library",
    ]:
        with pytest.raises(ValueError):
            configured_url(invalid)
    assert configured_url("http://localhost:13378/abs/") == "http://localhost:13378/abs"


def test_tag_metadata_outside_the_catalog_shape_is_still_read():
    regional = book()
    regional["media"]["metadata"]["language"] = "English (United States)"
    assert parse_item(regional).language is None
    named = book()
    named["media"]["metadata"]["language"] = "   English           spoken   "
    assert parse_item(named).language == "english spoken"
    unlabeled = book()
    unlabeled["media"]["metadata"]["language"] = ["en"]
    assert parse_item(unlabeled).language is None
    no_extension = book(ebook="epub")
    del no_extension["media"]["ebookFile"]["ebookFormat"]
    no_extension["media"]["ebookFile"]["metadata"]["ext"] = None
    assert parse_item(no_extension).ebook[0].format == ""


def test_bad_fields_are_dropped_and_named_instead_of_failing_the_item():
    value = book("odd")
    value["path"] = "/private/library/Folder Title"
    metadata = value["media"]["metadata"]
    metadata.update(
        title="",
        authors=[{"name": "Alex Morgan"}, {"name": None}],
        narrators=["Jordan Lee", 7],
        publishedYear="sometime",
        abridged="no",
        series="Harbor",
        isbn=["978"],
        descriptionPlain={"text": "x"},
    )
    value["oldLibraryItemId"] = "../bad"
    item = parse_item(value)
    assert item.title == "Folder Title"
    assert item.authors == ["Alex Morgan"] and item.narrators == ["Jordan Lee"]
    assert item.year is None and item.abridged is None and item.series == []
    assert item.identifiers == {} and item.description is None and item.old_id is None
    assert item.read_issues == [
        "title",
        "authors",
        "narrators",
        "description",
        "year",
        "abridged",
        "series",
        "isbn",
        "old_id",
    ]
    assert parse_item(book()).read_issues == []


def test_unreadable_placeholder_keeps_what_the_backend_said():
    value = book("lost")
    value["path"] = "/private/library/Lost Folder"
    value["libraryFiles"] = []
    value["media"]["metadata"]["title"] = None
    item = readable_item(value)
    assert item.unreadable and item.title == "Lost Folder"
    assert item.authors == ["Alex Morgan"] and item.path == "/private/library/Lost Folder"
    assert item.read_issues == ["Unlisted media file"]


async def test_library_items_that_cannot_be_read_are_kept_for_review(
    client, admin, database, caplog
):
    connection = await connect(client)
    fixture = ABSFixture({"one": book("one"), "two": book("two", narrator="Casey Reed")})
    await sync(client, connection, fixture, "readable-inventory")
    assert (await client.get("/api/library/assets")).json()["total"] == 2
    before = {
        asset["open_url"].rsplit("/", 1)[-1]: asset
        for asset in (await client.get("/api/library/assets")).json()["items"]
    }
    fixture.items["two"]["media"]["metadata"]["authors"] = [{"name": None}]
    fixture.items["three"] = book("three")
    fixture.items["three"]["path"] = "/private/library/Untagged Folder"
    fixture.items["three"]["media"]["metadata"]["title"] = ""
    fixture.items["four"] = book("four")
    fixture.items["four"]["path"] = "/private/library/Broken Folder"
    fixture.items["four"]["libraryFiles"] = []
    operation = await sync(client, connection, fixture, "malformed-items")
    async with database() as db:
        finished = await db.get(Operation, operation)
        assert finished.status == "completed"
        assert finished.message == "Synced 1 Audiobookshelf libraries. 3 items need review"
        assert finished.payload["review"] == {"total": 3, "needs_matching": 1, "read_issues": 3}
    assets = {
        asset["open_url"].rsplit("/", 1)[-1]: asset
        for asset in (await client.get("/api/library/assets")).json()["items"]
    }
    assert sorted(assets) == ["one", "three", "two"]
    # Bad author data does not undo the match made from the earlier readable scan.
    assert assets["two"]["work_ids"] == before["two"]["work_ids"]
    assert assets["two"]["match_status"] == before["two"]["match_status"]
    assert assets["two"]["read_issues"] == ["authors"]
    # Without a title, a new item is kept but never matched automatically.
    assert assets["three"]["title"] == "Untagged Folder"
    assert assets["three"]["match_status"] == "needs-review" and not assets["three"]["work_ids"]
    review = (await client.get("/api/library/review")).json()
    assert review["total"] == 3
    rows = sorted(
        (row["kind"], (row["asset"] or row["read_issue"])["title"]) for row in review["items"]
    )
    assert rows == [
        ("asset", "The First Harbor"),
        ("asset", "Untagged Folder"),
        ("read-issue", "The First Harbor"),
    ]
    unread = next(row["read_issue"] for row in review["items"] if row["kind"] == "read-issue")
    assert unread["authors"] == ["Alex Morgan"]
    assert unread["reasons"] == ["Unlisted media file"]
    assert unread["open_url"] == "https://books.test/abs/item/four"
    matching = (await client.get("/api/library/review?kind=needs-matching")).json()
    assert [row["asset"]["title"] for row in matching["items"]] == ["Untagged Folder"]
    assert (await client.get("/api/library/review?q=untagged")).json()["total"] == 1
    summary = (await client.get("/api/library/review/summary")).json()
    assert {key: summary[key] for key in ("total", "needs_matching", "read_issues")} == {
        "total": 3,
        "needs_matching": 1,
        "read_issues": 3,
    }
    assert {row["reason"]: row["count"] for row in summary["reasons"]} == {
        "authors": 1,
        "title": 1,
        "Unlisted media file": 1,
    }
    activity = (await client.get("/api/activity/page")).json()["items"]
    latest = next(item for item in activity if item["id"] == str(operation))
    assert latest["context"] == {"href": "/review", "label": "Review library items"}
    messages = [record.getMessage() for record in caplog.records]
    assert "Audiobookshelf item two was read without some fields (authors)" in messages
    assert "Audiobookshelf item four could not be read (Unlisted media file)" in messages
    assert not any("/private/" in message for message in messages)

    work_id = before["one"]["work_ids"][0]
    three = assets["three"]
    linked = await client.post(
        f"/api/library/assets/{three['id']}/match",
        json={"work_id": work_id, "expected_revision": three["match_revision"]},
    )
    assert linked.status_code == 204, linked.text
    assert (await client.get("/api/library/review?kind=needs-matching")).json()["total"] == 0

    # The manual match stands, but the missing title stays visible until the backend has one.
    fixture.items["two"]["media"]["metadata"]["authors"] = [{"name": "Alex Morgan"}]
    fixture.items["four"] = book("four")
    await sync(client, connection, fixture, "repaired-items")
    review = (await client.get("/api/library/review")).json()
    assert [row["asset"]["title"] for row in review["items"]] == ["Untagged Folder"]
    fixture.items["four"]["libraryFiles"] = []
    await sync(client, connection, fixture, "broken-again")
    assert (await client.get("/api/library/review?kind=read-issue")).json()["total"] == 2
    del fixture.items["four"]
    await sync(client, connection, fixture, "removed-item")
    assert (await client.get("/api/library/review/summary")).json()["total"] == 1


async def test_http_errors_redact_and_do_not_follow_redirects():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"Location": "http://elsewhere/secret"})

    async with Audiobookshelf(
        "http://abs.test", "secret", transport=httpx.MockTransport(handler)
    ) as api:
        with pytest.raises(AdapterError) as error:
            await api.libraries()
    assert error.value.kind == FailureKind.ROUTE
    assert "secret" not in str(error.value)
    assert len(calls) == 1


async def test_inventory_repeated_sync_versions_and_companions(client, admin, database):
    connection = await connect(client)
    fixture = ABSFixture(
        {
            "one": book("one", ebook="epub"),
            "two": book("two", ebook="pdf", narrator="Casey Reed"),
            "three": book("three"),
        }
    )
    await sync(client, connection, fixture, "first-inventory")
    works = (await client.get("/api/catalog/works")).json()["items"]
    assert len(works) == 1
    availability = works[0]["availability"]
    assert {
        key: availability[key] for key in ("owned", "ebook", "audio", "stale", "in_collection")
    } == {
        "owned": True,
        "ebook": True,
        "audio": True,
        "stale": False,
        "in_collection": False,
    }
    assert works[0]["publication_year"] is None  # Recording year is not the original work year.
    assets = (await client.get("/api/library/assets")).json()
    assert assets["total"] == 5
    # File paths are now an explicit part of the authenticated library-copy view.
    assert "/private/" not in json.dumps(
        [{key: value for key, value in item.items() if key != "files"} for item in assets["items"]]
    )
    assert all(
        item["open_url"].startswith("https://books.test/abs/item/") for item in assets["items"]
    )
    audio_versions = {item["version_id"] for item in assets["items"] if item["medium"] == "audio"}
    assert availability["audio_versions"] == 3
    assert availability["ebook_versions"] == 1
    assert availability["primary_audio_version_id"] in audio_versions
    assert availability["primary_ebook_version_id"] in {
        item["version_id"] for item in assets["items"] if item["medium"] == "ebook"
    }
    assert availability["audio_stale"] is False
    assert availability["ebook_stale"] is False
    assert len(audio_versions) == 3  # Same narrator alone cannot establish the same recording.
    companion = next(
        a for a in assets["items"] if a["open_url"].endswith("/two") and a["medium"] == "ebook"
    )
    assert companion["version_id"] is None and not companion["full_content"]
    await sync(client, connection, fixture, "second-inventory")
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Work)) == 1
        assert await db.scalar(select(func.count()).select_from(Version)) == 4
    fixture.items["one"]["media"]["metadata"]["narrators"] = ["Changed narrator"]
    await sync(client, connection, fixture, "changed-recording")
    assets = (await client.get("/api/library/assets")).json()["items"]
    changed = next(a for a in assets if a["open_url"].endswith("/one") and a["medium"] == "audio")
    assert changed["match_status"] == "needs-review"
    assert not changed["full_content"]


async def test_failed_sync_preserves_ownership_then_confirms_missing(client, admin, database):
    connection = await connect(client)
    fixture = ABSFixture({"one": book("one")})
    await sync(client, connection, fixture, "initial-inventory")
    fixture.fail_batch = True
    operation = await sync(client, connection, fixture, "failed-inventory")
    work = (await client.get("/api/catalog/works")).json()["items"][0]
    assert work["availability"]["owned"] and work["availability"]["stale"]
    async with database() as db:
        assert (await db.get(Operation, operation)).status == "failed"
    fixture.fail_batch = False
    fixture.items = {}
    await sync(client, connection, fixture, "missing-inventory")
    assert (await client.get("/api/library/assets")).json()["items"][0][
        "state"
    ] == "missing-suspected"
    async with database() as db, db.begin():
        await db.execute(
            update(LibraryAsset).values(missing_since=datetime.now(UTC) - timedelta(minutes=6))
        )
    await sync(client, connection, fixture, "confirmed-missing")
    assert (await client.get("/api/library/assets")).json()["items"][0][
        "state"
    ] == "missing-confirmed"
    assert not (await client.get(f"/api/catalog/works/{work['id']}")).json()["availability"][
        "owned"
    ]
    assert "/abs/api/items/one" in fixture.calls


async def test_generation_change_fences_inventory(client, admin, database):
    connection = await connect(client)
    fixture = ABSFixture({"one": book("one")})

    async def revoke():
        async with database() as db, db.begin():
            await db.execute(
                update(Integration)
                .where(Integration.id == UUID(connection))
                .values(credential_generation=1, lease_token=None, lease_until=None)
            )

    fixture.on_page = revoke
    operation = await sync(client, connection, fixture, "stale-generation")
    async with database() as db:
        assert (await db.get(Operation, operation)).status == "cancelled"
        assert await db.scalar(select(func.count()).select_from(LibraryAsset)) == 0


async def test_private_inventory_grants_and_manual_unmatch(client, admin, database):
    connection = await connect(client)
    fixture = ABSFixture({"one": book("one")})
    await sync(client, connection, fixture, "private-inventory")
    work = (await client.get("/api/catalog/works")).json()["items"][0]
    asset = (await client.get("/api/library/assets")).json()["items"][0]
    user = await client.post(
        "/api/auth/users",
        json={
            "username": "reader",
            "password": "a long reader password",
            "display_name": "Reader",
            "role": "member",
        },
    )
    assert user.status_code == 201, user.text
    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as reader:
        login = await reader.post(
            "/api/auth/login", json={"username": "reader", "password": "a long reader password"}
        )
        reader.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (await reader.get("/api/integrations")).status_code == 403
        assert (await reader.get("/api/catalog/works")).json()["total"] == 0
        assert (await reader.get(f"/api/catalog/works/{work['id']}")).status_code == 404
        assert (await reader.get("/api/library/assets")).json()["total"] == 0
        grant = f"/api/library/libraries/{asset['library_id']}/grants"
        assert (await client.put(grant, json={"user_ids": [user.json()["id"]]})).status_code == 204
        assert (await reader.get("/api/catalog/works")).json()["total"] == 1
        assert (await reader.get("/api/library/assets")).json()["total"] == 1
        assert (await client.put(grant, json={"user_ids": []})).status_code == 204
        assert (await reader.get("/api/catalog/works")).json()["total"] == 0
    match = f"/api/library/assets/{asset['id']}/match"
    assert (await client.post(match, json={"work_id": None})).status_code == 204
    await sync(client, connection, fixture, "manual-unmatch-persists")
    assert not (await client.get(f"/api/catalog/works/{work['id']}")).json()["availability"][
        "owned"
    ]
    assert (await client.post(match, json={"work_id": work["id"]})).status_code == 204
    await sync(client, connection, fixture, "manual-match-persists")
    assert (await client.get(f"/api/catalog/works/{work['id']}")).json()["availability"]["owned"]


async def test_connection_diagnostics_scope_change_and_disable(
    client, admin, database, monkeypatch
):
    import app.api.integrations as endpoint

    connection = await connect(client)
    fixture = ABSFixture({"one": book("one")})
    monkeypatch.setattr(endpoint, "Audiobookshelf", fixture.client)
    tested = await client.post(f"/api/integrations/{connection}/test")
    assert tested.status_code == 200
    assert tested.json()["version"] == "2.36.1"
    assert not tested.json()["scan_supported"]
    assert not tested.json()["last_success_at"]
    await sync(client, connection, fixture, "before-scope-change")
    fixture.scope = ["restricted-scope"]
    fixture.items = {}
    await sync(client, connection, fixture, "after-scope-change")
    assert (await client.get("/api/library/assets")).json()["items"][0][
        "state"
    ] == "scope-unavailable"
    fixture.hide_libraries = True
    await sync(client, connection, fixture, "library-not-accessible")
    assert not (await client.get("/api/library/libraries")).json()[0]["accessible"]
    assert (await client.get("/api/library/assets")).json()["total"] == 0
    disabled = await client.put(
        f"/api/integrations/{connection}",
        json={"name": "Home ABS", "base_url": "http://abs.test/abs", "enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["has_token"]
    assert (
        await client.post(
            f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": "disabled-sync"}
        )
    ).status_code == 409


async def test_partial_second_page_never_publishes_snapshot(client, admin, database):
    connection = await connect(client)
    fixture = ABSFixture({"one": book("one")})
    await sync(client, connection, fixture, "before-page-failure")
    fixture.items = {f"item-{i}": book(f"item-{i}") for i in range(101)}
    original = fixture.handle

    async def fail_page(request):
        if request.url.params.get("page") == "1":
            return httpx.Response(500)
        return await original(request)

    fixture.handle = fail_page
    operation = await sync(client, connection, fixture, "second-page-failure")
    async with database() as db:
        assert (await db.get(Operation, operation)).status == "failed"
        assert await db.scalar(select(func.count()).select_from(LibraryAsset)) == 1
    existing = (await client.get("/api/catalog/works")).json()["items"][0]
    assert existing["availability"]["owned"]
    assert existing["availability"]["stale"]


async def test_sync_coalesces_live_jobs_recovers_terminal_jobs(client, admin, database):
    import asyncio

    from sqlalchemy import text

    connection = await connect(client)
    responses = await asyncio.gather(
        *[
            client.post(
                f"/api/integrations/{connection}/sync",
                headers={"Idempotency-Key": f"parallel-sync-{i}"},
            )
            for i in range(5)
        ]
    )
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    identifier = UUID(responses[0].json()["id"])
    async with database() as db, db.begin():
        operation = await db.get(Operation, identifier)
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status = 'failed' WHERE id = :id"),
            {"id": operation.job_id},
        )
    response = await client.post(
        f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": "recover-exhausted-job"}
    )
    assert response.status_code == 202
    assert response.json()["id"] != str(identifier)
    async with database() as db:
        assert (await db.get(Operation, identifier)).status == "failed"


async def test_expired_run_is_reconciled_and_stale_owner_cannot_finish(client, admin, database):
    import asyncio
    from uuid import uuid4

    from app.db.models import InventoryObservation, InventoryRun

    connection = await connect(client)
    fixture = ABSFixture({"one": book("one")})
    response = await client.post(
        f"/api/integrations/{connection}/sync",
        headers={"Idempotency-Key": "reclaim-expired-worker"},
    )
    operation = UUID(response.json()["id"])
    async with database() as db, db.begin():
        integration = await db.get(Integration, UUID(connection))
        integration.lease_token = uuid4()
        integration.lease_until = datetime.now(UTC) - timedelta(minutes=4)
        run = InventoryRun(
            integration_id=integration.id, operation_id=operation, credential_generation=0
        )
        db.add(run)
        await db.flush()
        abandoned_id = run.id
        db.add(
            InventoryObservation(
                run_id=run.id, library_external_id="old", item_external_id="old", snapshot={}
            )
        )
    await synchronize(operation, client_factory=fixture.client)
    async with database() as db:
        assert (await db.get(InventoryRun, abandoned_id)).status == "interrupted"
        assert await db.scalar(select(func.count()).select_from(InventoryObservation)) == 0
        assert (await db.get(Operation, operation)).status == "completed"
    # A second live attempt is rejected without stealing its lease.
    response = await client.post(
        f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": "active-worker-claim"}
    )
    operation = UUID(response.json()["id"])
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop_at_page():
        entered.set()
        await release.wait()

    fixture.on_page = stop_at_page
    active = asyncio.create_task(synchronize(operation, client_factory=fixture.client))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        with pytest.raises(AdapterError) as error:
            await synchronize(operation, client_factory=fixture.client)
        assert error.value.kind == FailureKind.UNAVAILABLE
    finally:
        release.set()
        await asyncio.wait_for(active, timeout=5)
    async with database() as db:
        assert (await db.get(Operation, operation)).status == "completed"


async def test_stale_owner_cannot_overwrite_replacement_run(client, admin, database):
    import asyncio

    connection = await connect(client)
    old_fixture = ABSFixture({"old": book("old")})
    fresh_fixture = ABSFixture({"new": book("new")})
    response = await client.post(
        f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": "replace-stale-owner"}
    )
    operation = UUID(response.json()["id"])
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop():
        entered.set()
        await release.wait()

    old_fixture.on_page = stop
    old_worker = asyncio.create_task(synchronize(operation, client_factory=old_fixture.client))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        async with database() as db, db.begin():
            await db.execute(
                update(Integration)
                .where(Integration.id == UUID(connection))
                .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
            )
        await synchronize(operation, client_factory=fresh_fixture.client)
    finally:
        release.set()
        await asyncio.wait_for(old_worker, timeout=5)
    async with database() as db:
        assert (await db.get(Operation, operation)).status == "completed"
        assets = (await db.scalars(select(LibraryAsset))).all()
        assert [asset.external_id for asset in assets] == ["new"]


async def test_reused_key_across_commands_is_a_conflict(client, admin):
    import asyncio

    connection = await connect(client)
    responses = await asyncio.gather(
        client.post(
            f"/api/integrations/{connection}/sync", headers={"Idempotency-Key": "same-command-key"}
        ),
        client.post("/api/system/probe", headers={"Idempotency-Key": "same-command-key"}),
    )
    assert sorted(response.status_code for response in responses) == [202, 409]


async def test_access_revocation_during_sync_hides_cached_library(client, admin):
    connection = await connect(client)
    fixture = ABSFixture({"one": book("one")})
    await sync(client, connection, fixture, "initial-permitted-sync")

    async def revoke():
        fixture.scope = ["revoked"]

    fixture.on_page = revoke
    await sync(client, connection, fixture, "permissions-revoked-mid-sync")
    assert (await client.get("/api/library/assets")).json()["total"] == 0
    assert not (await client.get("/api/library/libraries")).json()[0]["accessible"]


async def test_move_between_libraries_retains_work_and_recording(client, admin, database):
    connection = await connect(client)
    fixture = ABSFixture({"one": book("one")})
    await sync(client, connection, fixture, "before-library-move")
    before = (await client.get("/api/library/assets")).json()["items"][0]
    fixture.items["one"]["libraryId"] = "library-two"
    original = fixture.handle

    async def moved(request):
        path = request.url.path.removeprefix("/abs/")
        if path == "api/libraries":
            return httpx.Response(
                200,
                json={
                    "libraries": [
                        {"id": "library-one", "name": "Old library", "mediaType": "book"},
                        {"id": "library-two", "name": "New library", "mediaType": "book"},
                    ]
                },
            )
        if path == "api/libraries/library-one/items":
            return httpx.Response(200, json={"results": [], "total": 0})
        if path == "api/libraries/library-two/items":
            return httpx.Response(
                200, json={"results": [{"id": "one", "updatedAt": 1}], "total": 1}
            )
        return await original(request)

    fixture.handle = moved
    await sync(client, connection, fixture, "after-library-move")
    assets = (await client.get("/api/library/assets")).json()["items"]
    assert len(assets) == 2
    assert {asset["state"] for asset in assets} == {"present", "moved"}
    assert {asset["version_id"] for asset in assets} == {before["version_id"]}
    assert all(asset["work_ids"] == before["work_ids"] for asset in assets)


async def test_due_inventory_scheduler_preserves_single_active_job(client, admin, database):
    from sqlalchemy import text

    from app.jobs.tasks import schedule_inventory

    connection = await connect(client)
    async with database() as db, db.begin():
        await db.execute(
            update(Integration)
            .where(Integration.id == UUID(connection))
            .values(next_sync_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await schedule_inventory(1789672200)
    await schedule_inventory(1789672200)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 1
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM book_queue.procrastinate_jobs "
                    "WHERE task_name = 'library.sync'"
                )
            )
            == 1
        )
        assert (await db.get(Integration, UUID(connection))).next_sync_at > datetime.now(UTC)


async def test_connection_preflight_counts_and_rejects_bad_credentials(client, admin, monkeypatch):
    fixture = ABSFixture({"one": book("one"), "two": book("two")})
    monkeypatch.setattr("app.api.integrations.Audiobookshelf", fixture.client)
    body = {
        "name": "Checked library",
        "base_url": "http://abs.test/abs",
        "token": "private-abs-token",
    }
    checked = await client.post("/api/integrations/check", json=body)
    assert checked.status_code == 200
    assert checked.json()["library_count"] == 1
    assert checked.json()["book_count"] == 2
    assert (await client.get("/api/integrations")).json() == []
    saved = await client.post("/api/integrations", json=body)
    assert saved.status_code == 201
    assert saved.json()["status"] == "connected"
    assert saved.json()["book_count"] == 2

    async def fail(request):
        return httpx.Response(401)

    monkeypatch.setattr(
        "app.api.integrations.Audiobookshelf",
        lambda endpoint, token: Audiobookshelf(
            endpoint, token, transport=httpx.MockTransport(fail)
        ),
    )
    assert (await client.post("/api/integrations/check", json=body)).status_code == 422
    assert (await client.post("/api/integrations", json=body)).status_code == 422
    assert len((await client.get("/api/integrations")).json()) == 1
