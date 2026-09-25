# ruff: noqa: F401, F811
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    DownloadAttempt,
    Integration,
    Operation,
    SourceResult,
)
from app.domain import automatic_selection, quick_add
from tests.integration.test_acquisition import catalog
from tests.integration.test_acquisition_defaults import save
from tests.integration.test_acquisition_selections import selection_route
from tests.integration.test_automatic_dispatch import authorized
from tests.integration.test_automatic_selection import source

pytestmark = pytest.mark.integration


async def defaults(client, fixture, **extra):
    await save(
        client,
        {
            "desired_media": "audio",
            "downloader_id": fixture["body"]["downloader_id"],
            "audio_destination_id": fixture["body"]["destination_id"],
            **extra,
        },
    )


async def add(client, work, mode=None, key=None):
    return await client.post(
        "/api/requests/quick-add",
        json={
            "work_id": str(work),
            "specification": {"mode": mode} if mode else {},
        },
        headers={"Idempotency-Key": key or str(uuid4())},
    )


async def complete_search(database, operation_id, fixture):
    async with database() as db, db.begin():
        parent = await db.get(Operation, operation_id)
        search = await db.get(Operation, UUID(parent.payload["search_id"]))
        payload = deepcopy(search.payload)
        payload["workers"] = {}
        payload.pop("catalog_preparation", None)
        for unit in payload["sources"].values():
            unit.update(state="completed", count=1, message="Fixture results")
        search.payload = payload
        search.status = "completed"
        original = await db.get(SourceResult, fixture["result"])
        db.add(
            SourceResult(
                owner_id=original.owner_id,
                operation_id=search.id,
                source_key=original.source_key,
                source_generation=original.source_generation,
                expires_at=original.expires_at,
                encrypted_reference=original.encrypted_reference,
                release_snapshot=original.release_snapshot,
            )
        )


@pytest.mark.parametrize("automatic_folders", [False, True])
async def test_quick_add_inherits_preferences_and_downloads_once(
    client, database, authorized, catalog, automatic_folders
):
    if automatic_folders:
        await save(client, {"desired_media": "audio", "audio_formats": ["m4b", "mp3"]})
    else:
        await defaults(client, authorized, audio_formats=["m4b", "mp3"])
    response = await add(client, catalog["work"], key="quick-add-one-click")
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    repeated = await add(client, catalog["work"], key="quick-add-one-click")
    assert repeated.json()["id"] == str(identifier)
    repeated = await add(client, catalog["work"])
    assert repeated.json()["id"] == str(identifier)
    async with database() as db:
        op = await db.get(Operation, identifier)
        intent = await db.get(AcquisitionIntent, UUID(op.payload["intent_id"]))
        assert intent.specification["mode"] == "audio"
        assert intent.release_policy["preferences"]["audio_formats"] == ["m4b", "mp3"]
    await complete_search(database, identifier, authorized)
    await quick_add.run(identifier)
    async with database() as db:
        op = await db.get(Operation, identifier)
        assert op.status == "running", op.message
        child_id = UUID(op.payload["slots"]["audio"]["operation_id"])
        child = await db.get(Operation, child_id)
        assert child.payload["command"]["download_when_ready"] is True
    await automatic_selection.run(child_id)
    await quick_add.run(identifier)
    await quick_add.run(identifier)
    async with database() as db:
        op = await db.get(Operation, identifier)
        assert op.status == "completed", op.message
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
    latest = await client.get(f"/api/requests/quick-add/latest/{catalog['work']}")
    assert latest.json()["status"] == "completed"


async def test_quick_add_uses_torrent_default_despite_legacy_usenet_primary(
    client, database, authorized, catalog
):
    async with database() as db, db.begin():
        other = Integration(
            name="Usenet",
            kind="sabnzbd",
            base_url="http://unused.invalid",
            encrypted_secrets="unused",
            config={"save_path": "/not-mapped", "mappings": []},
            status="connected",
            enabled=True,
        )
        db.add(other)
        await db.flush()
        other_id = str(other.id)
    await defaults(client, authorized, downloader_id=other_id)
    response = await add(client, catalog["work"])
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    async with database() as db:
        operation = await db.get(Operation, identifier)
        assert operation.payload["routes"]["downloader_id"] == authorized["body"]["downloader_id"]
    await complete_search(database, identifier, authorized)
    await quick_add.run(identifier)
    async with database() as db:
        operation = await db.get(Operation, identifier)
        child_id = UUID(operation.payload["slots"]["audio"]["operation_id"])
    await automatic_selection.run(child_id)
    await quick_add.run(identifier)
    receipt = (await client.get(f"/api/requests/quick-add/latest/{catalog['work']}")).json()
    assert receipt["status"] == "completed", receipt
    assert receipt["request_id"]
    assert receipt["source_checks"][0]["source"] == "MAM"
    assert receipt["source_checks"][0]["status"] == "completed"


async def test_quick_add_feedback_explains_older_candidate_rejections(
    client, database, authorized, catalog
):
    await defaults(client, authorized)
    response = await add(client, catalog["work"])
    identifier = UUID(response.json()["id"])
    await complete_search(database, identifier, authorized)
    await quick_add.run(identifier)
    async with database() as db, db.begin():
        operation = await db.get(Operation, identifier)
        child = await db.get(Operation, UUID(operation.payload["slots"]["audio"]["operation_id"]))
        child.status = "held"
        child.message = "No eligible release found within this page and inspection budget"
        child.payload = {
            **child.payload,
            "decisions": [
                {
                    "result_id": str(authorized["result"]),
                    "reasons": ["No ready torrent download route"],
                    "inspected": True,
                }
            ],
            "inspected": [str(authorized["result"])],
        }
    await quick_add.run(identifier)
    receipt = (await client.get(f"/api/requests/quick-add/latest/{catalog['work']}")).json()
    assert receipt["status"] == "held"
    check = receipt["source_checks"][0]
    assert check["source"] == "MAM" and check["slot"] == "audio"
    assert check["reasons"] == ["No ready torrent download route"]
    assert check["candidates"] == 1
    assert "No ready torrent download route" in check["message"]
    assert "inspection budget" not in check["message"]


async def test_quick_add_explicit_medium_overrides_default_without_changing_it(
    client, database, authorized, catalog
):
    await defaults(client, authorized, desired_media="both")
    response = await add(client, catalog["work"], "audio")
    assert response.status_code == 202, response.text
    async with database() as db:
        op = await db.get(Operation, UUID(response.json()["id"]))
        intent = await db.get(AcquisitionIntent, UUID(op.payload["intent_id"]))
        assert intent.specification["mode"] == "audio"
        assert list(op.payload["slots"]) == ["audio"]
    preferences = (await client.get("/api/acquisition/preferences/personal")).json()
    assert preferences["effective"]["desired_media"] == "both"


async def test_quick_add_missing_routes_rolls_back_and_rejects_key_reuse(
    client, database, admin, catalog
):
    await save(client, {"desired_media": "audio"})
    response = await add(client, catalog["work"])
    assert response.status_code == 422, response.text
    assert "downloader" in response.text.lower()
    async with database() as db:
        assert not await db.scalar(select(Operation.id).where(Operation.kind == quick_add.KIND))
        assert not await db.scalar(select(DownloadAttempt.id))


async def test_save_torrent_is_owner_scoped(client, database, authorized):
    response = await client.get(f"/api/source-artifacts/{authorized['artifact']}/torrent")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-bittorrent"
    assert response.content.startswith(b"d")
    assert "attachment" in response.headers["content-disposition"]
    response = await client.get(f"/api/source-artifacts/{uuid4()}/torrent")
    assert response.status_code == 404


async def test_both_skips_owned_ebook_without_requiring_ebook_route(
    client, database, authorized, catalog
):
    await defaults(client, authorized, desired_media="both")
    response = await add(client, catalog["work"])
    assert response.status_code == 202, response.text
    async with database() as db:
        op = await db.get(Operation, UUID(response.json()["id"]))
        assert list(op.payload["slots"]) == ["audio"]
        intent = await db.get(AcquisitionIntent, UUID(op.payload["intent_id"]))
        assert intent.specification["mode"] == "both"


async def test_both_missing_media_create_independent_automatic_selections(
    client, database, authorized, catalog, monkeypatch, tmp_path
):
    from app.config import get_settings
    from app.db.models import ImportDestination, LibraryAsset
    from app.importing.destinations import destination_configuration
    from app.importing.naming import fingerprint

    settings = get_settings()
    (tmp_path / "ebooks").mkdir()
    monkeypatch.setattr(
        settings,
        "import_destinations",
        {**settings.import_destinations, "ebook": tmp_path / "ebooks"},
    )
    async with database() as db, db.begin():
        (await db.get(LibraryAsset, catalog["asset"])).full_content = False
        audio = await db.get(ImportDestination, UUID(authorized["body"]["destination_id"]))
        # Updating watched roots changes the audio route revision too.
        audio_revision = fingerprint(await destination_configuration(db, audio))
        audio.probe = {**audio.probe, "configuration_revision": audio_revision}
        ebook = ImportDestination(
            root_key="ebook",
            library_id=audio.library_id,
            medium="ebook",
            backend_path="/ebooks",
        )
        db.add(ebook)
        await db.flush()
        revision = fingerprint(await destination_configuration(db, ebook))
        ebook.probe = {**audio.probe, "configuration_revision": revision}
        ebook_id = str(ebook.id)
    approved_audio = await client.put(
        f"/api/organization/destinations/{authorized['body']['destination_id']}/automatic-import",
        json={
            "enabled": True,
            "expected_generation": 1,
            "destination_revision": audio_revision,
        },
    )
    assert approved_audio.status_code == 200, approved_audio.text
    approved = await client.put(
        f"/api/organization/destinations/{ebook_id}/automatic-import",
        json={
            "enabled": True,
            "expected_generation": 0,
            "destination_revision": revision,
        },
    )
    assert approved.status_code == 200, approved.text
    await defaults(client, authorized, desired_media="both", ebook_destination_id=ebook_id)
    response = await add(client, catalog["work"])
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    await complete_search(database, identifier, authorized)
    await quick_add.run(identifier)
    async with database() as db:
        op = await db.get(Operation, identifier)
        assert set(op.payload["slots"]) == {"ebook", "audio"}
        for medium, progress in op.payload["slots"].items():
            assert "operation_id" in progress, progress
            child = await db.get(Operation, UUID(progress["operation_id"]))
            assert child.payload["command"]["slot"] == medium
            assert child.payload["command"]["download_when_ready"] is True
    # No ebook candidate: that slot must not prevent the audiobook from downloading.
    for progress in op.payload["slots"].values():
        await automatic_selection.run(UUID(progress["operation_id"]))
    await quick_add.run(identifier)
    async with database() as db:
        op = await db.get(Operation, identifier)
        assert op.status == "held", op.message
        assert op.payload["slots"]["ebook"]["failed"]
        assert not op.payload["slots"]["audio"]["failed"]
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1


async def test_cancelled_request_during_search_does_not_download(
    client, database, authorized, catalog
):
    from app.db.models import AcquisitionReason

    await defaults(client, authorized)
    response = await add(client, catalog["work"], key="quick-cancel-fixture")
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    changed = await add(client, catalog["work"], "ebook", key="quick-cancel-fixture")
    assert changed.status_code == 409
    async with database() as db, db.begin():
        op = await db.get(Operation, identifier)
        for reason in await db.scalars(
            select(AcquisitionReason).where(
                AcquisitionReason.intent_id == UUID(op.payload["intent_id"])
            )
        ):
            reason.active = False
    await complete_search(database, identifier, authorized)
    await quick_add.run(identifier)
    async with database() as db:
        op = await db.get(Operation, identifier)
        assert op.status == "held", op.message
        assert not await db.scalar(select(DownloadAttempt.id))


@pytest.mark.parametrize("usenet_primary", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("automatic_folders", [False, True])
async def test_clicked_release_download_is_pinned_and_idempotent(
    client, database, authorized, blocked, automatic_folders, usenet_primary
):
    if not automatic_folders:
        await defaults(client, authorized)
    if usenet_primary:
        async with database() as db, db.begin():
            other_client = Integration(
                name="Usenet",
                kind="sabnzbd",
                base_url="http://unused.invalid",
                encrypted_secrets="unused",
                status="connected",
                enabled=True,
            )
            db.add(other_client)
            await db.flush()
            usenet_id = str(other_client.id)
        await save(client, {"downloader_id": usenet_id})
    profiles = (await client.get("/api/acquisition/profiles")).json()
    async with database() as db, db.begin():
        search = await db.get(Operation, authorized["search"])
        search.payload = {
            **search.payload,
            "profile": profiles[0],
            "workers": {},
            "sources": {},
            "query": "Harbor",
            "medium": "all",
            "offset": 0,
        }
        original = await db.get(SourceResult, authorized["result"])
        other = SourceResult(
            owner_id=original.owner_id,
            operation_id=original.operation_id,
            source_key=original.source_key,
            source_generation=original.source_generation,
            expires_at=original.expires_at,
            encrypted_reference=original.encrypted_reference,
            release_snapshot={**original.release_snapshot, "seeders": 99999, "source_id": "999"},
        )
        db.add(other)
        if blocked:
            original.release_snapshot = {
                **original.release_snapshot,
                "authors": ["Wrong Author"],
                "title": "Unrelated Book",
            }
    path = f"/api/source-searches/{authorized['search']}/results/{authorized['result']}/download"
    response = await client.post(path, headers={"Idempotency-Key": "clicked-release-download"})
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    await automatic_selection.run(identifier)
    repeated = await client.post(path, headers={"Idempotency-Key": "clicked-release-download"})
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["id"] == str(identifier)
    async with database() as db:
        operation = await db.get(Operation, identifier)
        assert operation.status == ("held" if blocked else "completed"), operation.message
        assert operation.payload["command"]["result_id"] == str(authorized["result"])
        assert operation.payload["command"]["downloader_id"] == authorized["body"]["downloader_id"]
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == (
            0 if blocked else 1
        )
    assert authorized["resolver"].calls == ([] if blocked else [authorized["result"]])
    status_response = await client.get(f"/api/source-searches/{authorized['search']}")
    assert status_response.status_code == 200, status_response.text
    items = {item["id"]: item for item in status_response.json()["items"]}
    saved = items[str(authorized["result"])]["download"]
    assert saved["state"] == ("failed" if blocked else "queued")
    assert saved["prevent_download"] is (not blocked)
    assert items[str(other.id)]["download"] is None
    request = await client.get(f"/api/requests/{saved['request_id']}")
    assert request.status_code == 200, request.text
    target = next(t for t in request.json()["targets"] if t["slot"] == "audio")
    if blocked:
        assert saved["reasons"]
        assert "This release could not be downloaded" in saved["message"]
        assert target["selection_status"] == "held"
        assert target["message"] == saved["message"]
    else:
        assert target["attempt_state"] == "queued"
        async with database() as db, db.begin():
            attempt = await db.get(DownloadAttempt, UUID(saved["attempt_id"]))
            attempt.state = "downloading"
            attempt.observation = {"progress": 0.42}
        fresh = (await client.get(f"/api/source-searches/{authorized['search']}")).json()
        downloading = next(
            i["download"] for i in fresh["items"] if i["id"] == str(authorized["result"])
        )
        assert downloading["state"] == "downloading"
        assert downloading["progress"] == 0.42
        async with database() as db, db.begin():
            attempt = await db.get(DownloadAttempt, UUID(saved["attempt_id"]))
            attempt.state = "complete"
            original = await db.get(SourceResult, authorized["result"])
            # A refreshed search result has a new ID but the same source identity.
            clone = SourceResult(
                owner_id=original.owner_id,
                operation_id=original.operation_id,
                source_key=original.source_key,
                source_generation=original.source_generation,
                expires_at=original.expires_at,
                encrypted_reference=original.encrypted_reference,
                release_snapshot=original.release_snapshot,
            )
            db.add(clone)
            await db.flush()
            clone_id = str(clone.id)
        fresh = (await client.get(f"/api/source-searches/{authorized['search']}")).json()
        completed = next(i["download"] for i in fresh["items"] if i["id"] == clone_id)
        assert completed["state"] == "downloaded"
        assert completed["prevent_download"]
        assert "Waiting for library import" in completed["message"]

    # An independent list reason keeps this request's source receipt current.
    book_list = (await client.post("/api/lists", json={"name": "Still wanted"})).json()
    listed = await client.post(
        f"/api/lists/{book_list['id']}/entries",
        json={"work_id": request.json()["work_id"]},
    )
    assert listed.status_code == 204, listed.text
    async with database() as db, db.begin():
        reason = AcquisitionReason(
            intent_id=UUID(saved["request_id"]),
            kind="list",
            reference=book_list["id"],
            list_id=UUID(book_list["id"]),
        )
        db.add(reason)
        await db.flush()
        list_reason_id = reason.id
    for reason in request.json()["reasons"]:
        response = await client.delete(
            f"/api/requests/{saved['request_id']}/reasons/{reason['id']}"
        )
        assert response.status_code == 200, response.text
    fresh = (await client.get(f"/api/source-searches/{authorized['search']}")).json()
    status = next(i["download"] for i in fresh["items"] if i["id"] == str(authorized["result"]))
    assert status["state"] == ("failed" if blocked else "downloaded")

    response = await client.delete(f"/api/requests/{saved['request_id']}/reasons/{list_reason_id}")
    assert response.status_code == 200, response.text
    fresh = (await client.get(f"/api/source-searches/{authorized['search']}")).json()
    status = next(i["download"] for i in fresh["items"] if i["id"] == str(authorized["result"]))
    if blocked:
        assert status is None
    else:
        assert status["state"] == "downloaded"
        assert status["prevent_download"]


async def test_clicked_release_rejects_result_from_another_search(client, database, authorized):
    response = await client.post(
        f"/api/source-searches/{authorized['search']}/results/{uuid4()}/download",
        headers={"Idempotency-Key": "unknown-clicked-release"},
    )
    assert response.status_code == 404, response.text
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 0


async def test_clicked_release_missing_torrent_route_explains_the_required_setup(
    client, database, authorized, monkeypatch
):
    from unittest.mock import AsyncMock

    await defaults(client, authorized)
    profiles = (await client.get("/api/acquisition/profiles")).json()
    async with database() as db, db.begin():
        search = await db.get(Operation, authorized["search"])
        search.payload = {**search.payload, "profile": profiles[0]}
    monkeypatch.setattr(automatic_selection, "matching_route", AsyncMock(return_value=None))
    response = await client.post(
        f"/api/source-searches/{authorized['search']}/results/{authorized['result']}/download",
        headers={"Idempotency-Key": "clicked-release-missing-torrent-route"},
    )
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    await automatic_selection.run(identifier)
    await automatic_selection.run(identifier)
    receipt = (await client.get(f"/api/acquisition/automatic-selections/{identifier}")).json()
    assert receipt["status"] == "held"
    assert "No ready torrent download route" in receipt["message"]
    assert "download folder" in receipt["message"]
    assert "import destination" in receipt["message"]
    assert "inspection budget" not in receipt["message"]
    assert authorized["resolver"].calls == []
