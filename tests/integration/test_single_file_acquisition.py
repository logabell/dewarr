# ruff: noqa: F811
"""Reviewed single-file acquisition through real file publication and fixture ABS."""

import asyncio
import base64
import errno
import hashlib
import json
from contextlib import aclosing
from datetime import UTC, datetime
from uuid import UUID, uuid4

import libtorrent as lt
import pytest
from sqlalchemy import delete, func, select

from app.adapters.mam import release
from app.adapters.torrent_descriptor import inspect_torrent
from app.config import get_settings
from app.db.models import (
    AcquisitionReason,
    AcquisitionReservation,
    AcquisitionSelection,
    AuditEvent,
    AutomaticImport,
    AutomaticImportPolicy,
    CatalogAccount,
    DownloadAttempt,
    DownloadFulfillment,
    DownloadIdentityClaim,
    FrozenImportPlan,
    ImportEntry,
    ImportRun,
    Integration,
    LibraryGrant,
    MetadataSettings,
    Operation,
    ProviderObject,
    SourceArtifact,
    SourceConnection,
    User,
    Version,
    WorkMetadataSource,
)
from app.domain import download_attempts as downloads
from app.importing import automatic, catalog_resolution, execution
from app.jobs.queue import get_queue
from app.security import encrypt_secrets
from tests.catalog_resolution_fixture import resolution_provider  # noqa: F401
from tests.integration.test_acquisition import body, request
from tests.integration.test_acquisition_selections import prepare
from tests.integration.test_download_attempts import Client
from tests.integration.test_download_attempts import start as start_download
from tests.integration.test_download_reviews import (
    admin_client,
    review_account,  # noqa: F401
)
from tests.integration.test_import_destinations import route as destination_route  # noqa: F401
from tests.integration.test_import_destinations import start_probe
from tests.integration.test_import_execution import ready_route  # noqa: F401
from tests.integration.test_import_execution import start as start_import
from tests.integration.test_inspection_matching import edition
from tests.mam_fixture import release_row
from tests.media_fixtures import audio, epub

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("save_relative", ["", "nested"])
@pytest.mark.parametrize(
    "handoff",
    [
        False,
        True,
        "grant",
        "withdraw",
        "requester",
        "reviewer",
        "automatic",
        "automatic-audio",
        "automatic-provider",
        "automatic-provider-audio",
        "automatic-provider-revoked",
        "automatic-provider-rotation",
        "automatic-provider-conflict",
        "automatic-provider-retry",
        "automatic-provider-settings",
        "automatic-unmatched",
        "automatic-linked-audio",
        "automatic-manifest",
        "automatic-disable",
        "automatic-sample",
        "automatic-ambiguous",
    ],
)
async def test_single_epub_download_to_confirmed_library_keeps_neighbor_private(
    client,
    admin,
    database,
    ready_route,
    monkeypatch,
    save_relative,
    handoff,
    review_account,
    resolution_provider,
):
    route = ready_route
    automatic_mode = isinstance(handoff, str) and handoff.startswith("automatic")
    old = route["plan"]["document"]["groups"][0]
    provider_mode = isinstance(handoff, str) and handoff.startswith("automatic-provider")
    success = {
        "automatic",
        "automatic-audio",
        "automatic-provider",
        "automatic-provider-audio",
        "automatic-provider-retry",
        "automatic-unmatched",
        "automatic-linked-audio",
    }
    medium = (
        "audio"
        if handoff in {"automatic-audio", "automatic-provider-audio", "automatic-linked-audio"}
        else "ebook"
    )
    name = "selected.mp3" if medium == "audio" else "selected.epub"
    source = route["source"] / save_relative / name
    if medium == "audio":
        audio(
            source,
            tags={
                "language": "en",
                **({} if handoff == "automatic-linked-audio" else {"isbn": "9781234567897"}),
            },
        )
        seeded = await prepare_audio_route(client, database, route, old["work_id"], source)
        if provider_mode:
            async with database() as db, db.begin():
                await db.execute(
                    delete(ProviderObject).where(ProviderObject.id == seeded["provider"])
                )
                await db.execute(
                    delete(WorkMetadataSource).where(WorkMetadataSource.id == seeded["source"])
                )
    else:
        epub(
            source,
            isbn="9781234567897" if automatic_mode and handoff != "automatic-unmatched" else None,
        )
    epub(source.parent / "unrelated.epub", title="Not part of this torrent")
    original = source.read_bytes()
    pieces = b"".join(
        hashlib.sha1(original[pos : pos + 16384]).digest() for pos in range(0, len(original), 16384)
    )
    raw = lt.bencode(
        {
            b"info": {
                b"name": name.encode(),
                b"length": len(original),
                b"piece length": 16384,
                b"pieces": pieces,
            }
        }
    )
    descriptor = await inspect_torrent(raw)
    async with database() as db, db.begin():
        db.add(
            SourceConnection(
                key="mam",
                base_url="https://mam.test",
                encrypted_secrets=encrypt_secrets({"mam_id": "fixture-only"}),
            )
        )
        await db.flush()
        downloader = Integration(
            kind="qbittorrent",
            name="Fixture download client",
            base_url="http://qbit.test",
            encrypted_secrets=encrypt_secrets({"username": "fixture", "password": "fixture"}),
            credential_generation=1,
            status="connected",
            config={
                "save_path": "/downloads" + ("/" + save_relative if save_relative else ""),
                "category": "book-search",
                "mappings": [
                    {
                        "download_root": "/downloads",
                        "source_key": "fixture",
                        "source_path": str(route["source"]),
                    }
                ],
            },
        )
        artifact = SourceArtifact(
            owner_id=UUID(admin["id"]),
            source_key="mam",
            source_id="502",
            source_generation=1,
            sha256=descriptor.artifact_sha256,
            descriptor=descriptor.model_dump(mode="json"),
            encrypted_content=encrypt_secrets({"torrent": base64.b64encode(raw).decode()}),
            release_snapshot=release(
                release_row(
                    id=502,
                    title="First Harbor sample"
                    if handoff == "automatic-sample"
                    else "First Harbor",
                    main_cat=13 if medium == "audio" else 14,
                    filetype="MP3" if medium == "audio" else "EPUB",
                    narrator_info='{"1":"Jordan Lee"}' if medium == "audio" else "{}",
                ),
                datetime.now(UTC),
            ).model_dump(mode="json"),
        )
        db.add_all([downloader, artifact])
        await db.flush()
        downloader_id, artifact_id = str(downloader.id), str(artifact.id)
    wanted = await request(
        client,
        body(
            {"work": old["work_id"]},
            medium,
            **{
                medium + "_library_id": route["library_id"],
                **({"required_narrators": ["Jordan Lee"]} if medium == "audio" else {}),
            },
        ),
    )
    selected_response = await prepare(
        client,
        {
            "intent_id": wanted["request"]["id"],
            "slot": medium,
            "artifact_id": artifact_id,
            "downloader_id": downloader_id,
            "downloader_generation": 1,
            "destination_id": route["destination"]["id"],
            "destination_revision": route["destination"]["revision"],
            "confirmed_work_id": old["work_id"],
        },
    )
    assert selected_response.status_code == 201, selected_response.text
    owner_client = client
    if automatic_mode:
        if medium == "ebook" and not provider_mode:
            await edition(database, work_id=UUID(old["work_id"]))
        if handoff == "automatic-ambiguous":
            await edition(database, work_id=UUID(old["work_id"]))
        response = await review_account[0].put(
            f"/api/organization/destinations/{route['destination']['id']}/automatic-import",
            json={
                "enabled": True,
                "expected_generation": 0,
                "destination_revision": route["destination"]["revision"],
            },
        )
        assert response.status_code == 200 and response.json()["ready"], response.text
    if provider_mode:
        resolution_provider["medium"] = medium
        if handoff == "automatic-provider-conflict":
            resolution_provider["fault"] = "ambiguous"
        if handoff == "automatic-provider-retry":
            resolution_provider["fault"] = "quota"
        async with database() as db, db.begin():
            db.add(
                CatalogAccount(
                    user_id=UUID(admin["id"]),
                    encrypted_token=encrypt_secrets({"token": "requester-catalog-token"}),
                    generation=1,
                )
            )

        async def change_during_lookup():
            async with database() as db, db.begin():
                if handoff == "automatic-provider-revoked":
                    await db.execute(
                        delete(LibraryGrant).where(LibraryGrant.user_id == UUID(admin["id"]))
                    )
                elif handoff == "automatic-provider-rotation":
                    (await db.get(CatalogAccount, UUID(admin["id"]))).generation += 1
                elif handoff == "automatic-provider-settings":
                    db.add(MetadataSettings(id=1, preferences={"automatic_edition_lookup": False}))

        resolution_provider["hook"] = change_during_lookup
    if handoff:
        async with database() as db, db.begin():
            (await db.get(User, UUID(admin["id"]))).role = "member"
            db.add(LibraryGrant(user_id=UUID(admin["id"]), library_id=UUID(route["library_id"])))
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    qbit = Client(database, descriptor.model_dump(mode="json"))
    qbit.complete = True
    monkeypatch.setattr(downloads, "QbitClient", lambda *args: qbit)
    started = await start_download(client, selected_response.json())
    assert started.status_code == 202, started.text
    if handoff == "automatic-manifest":
        epub(source, title="Changed file size after download", isbn="9781234567897")
    if handoff == "automatic-disable":
        loop = asyncio.get_running_loop()
        original_publish = execution.publish_item

        async def disable():
            async with database() as db, db.begin():
                policy = await db.scalar(select(AutomaticImportPolicy))
                policy.enabled = False
                policy.generation += 1

        def publish(spec, *, checkpoint, publication_guard, **kwargs):
            def before(phase):
                if phase == "prepared":
                    asyncio.run_coroutine_threadsafe(disable(), loop).result(timeout=10)
                checkpoint(phase)

            return original_publish(
                spec, checkpoint=before, publication_guard=publication_guard, **kwargs
            )

        monkeypatch.setattr(execution, "publish_item", publish)
    if handoff == "automatic-linked-audio":
        original_publish = execution.publish_item

        def denied(*args, **kwargs):
            raise PermissionError(errno.EACCES, "private path must not appear", "/private/download")

        monkeypatch.setattr(execution, "publish_item", denied)
    await get_queue().run_worker_async(wait=False, concurrency=1)
    if handoff == "automatic-linked-audio":
        async with database() as db:
            auto = await db.scalar(select(AutomaticImport))
            entry = await db.scalar(select(ImportEntry))
            run = await db.get(ImportRun, entry.run_id)
            assert entry.state == "held" and "EACCES" in entry.message
            assert "/private" not in entry.message
        reviewer = review_account[0]
        endpoint = f"/api/organization/inspections/{auto.inspection_id}"
        context = await reviewer.get(endpoint)
        assert context.status_code == 200, context.text
        assert context.json()["plan_id"] == str(run.plan_id)
        assert context.json()["download"]["state"] == "held"
        assert context.json()["download"]["work_id"] == old["work_id"]
        assert not context.json()["download"]["can_retry"]  # Retry the existing entry.
        assert (await owner_client.get(endpoint)).status_code == 403
        assert (await reviewer.post(endpoint + "/retry")).status_code == 409
        monkeypatch.setattr(execution, "publish_item", original_publish)
        retried = await reviewer.post(
            f"/api/organization/imports/{run.id}/entries/{entry.id}/retry"
        )
        assert retried.status_code == 202, retried.text
        await get_queue().run_worker_async(wait=False, concurrency=1)
        context = (await reviewer.get(endpoint)).json()
        assert context["download"]["state"] == "complete", context
        assert context["plan_id"] == str(run.plan_id)
    if handoff == "automatic-provider-retry":
        async with database() as db, db.begin():
            auto = await db.scalar(select(AutomaticImport))
            resolved = await db.get(Operation, UUID(auto.evidence["catalog_resolution"]))
            assert resolved.status == "retrying" and resolved.payload["attempts"] == 1
            assert not await db.scalar(select(ImportRun.id))
            old_job = (await db.get(Operation, auto.operation_id)).job_id
            await automatic.recover(db, auto.id)
            assert (await db.get(Operation, auto.operation_id)).job_id == old_job
        resolution_provider["fault"] = None
        await asyncio.gather(*(catalog_resolution.resolve(resolved.id) for _ in range(2)))
        await get_queue().run_worker_async(wait=False, concurrency=1)
    if automatic_mode and handoff not in success:
        async with database() as db:
            auto = await db.scalar(select(AutomaticImport))
            if handoff == "automatic-disable":
                entries = list(await db.scalars(select(ImportEntry)))
                assert len(entries) == 1 and entries[0].state == "held"
            else:
                assert auto.state == "held", auto.message
                assert not await db.scalar(select(ImportEntry.id))
            assert not await db.scalar(select(DownloadFulfillment.id))
        assert qbit.calls.count("submit") == 1 and not list(route["target"].rglob("*.epub"))
        return
    if handoff in success:
        async with database() as db:
            auto = await db.scalar(select(AutomaticImport))
            assert auto.state == "importing", (auto.message, auto.evidence)
            plan = await db.get(
                FrozenImportPlan, (await db.get(ImportRun, auto.import_run_id)).plan_id
            )
            entries = list(await db.scalars(select(ImportEntry)))
            assert len(entries) == 1 and entries[0].state == "confirmed"
            if handoff in {"automatic-unmatched", "automatic-linked-audio"}:
                assert auto.evidence["linked_download"]["work_id"] == old["work_id"]
            else:
                assert plan.document["matching_evidence"]
            fulfilled = await db.scalar(select(DownloadFulfillment))
            assert fulfilled and fulfilled.import_entry_id == entries[0].id
            assert (await db.scalar(select(DownloadIdentityClaim))).active
            if provider_mode:
                resolved = await db.get(Operation, UUID(auto.evidence["catalog_resolution"]))
                assert resolved.status == "completed" and resolved.owner_id == UUID(admin["id"])
                assert await db.scalar(
                    select(WorkMetadataSource.id).where(
                        WorkMetadataSource.work_id == UUID(old["work_id"])
                    )
                )
                assert all(
                    auth == ("Bearer requester-catalog-token" if path == "/v1/graphql" else None)
                    for path, auth in resolution_provider["calls"]
                )
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(AuditEvent)
                        .where(AuditEvent.action == "metadata.import.resolved")
                    )
                    == 1
                )
        assert qbit.calls.count("submit") == 1
        output = list(route["target"].rglob("*.mp3" if medium == "audio" else "*.epub"))
        assert len(output) == 1 and output[0].stat().st_ino == source.stat().st_ino
        assert output[0].read_bytes() == original == source.read_bytes()
        assert (source.parent / "unrelated.epub").exists()
        await automatic.run(auto.id)
        assert qbit.calls.count("submit") == 1
        activity = (await owner_client.get(f"/api/acquisition/downloads/{auto.attempt_id}")).json()
        assert activity["fulfillment"]["available_now"] and activity["inspection_id"] is None
        fulfilled_request = (
            await owner_client.get(f"/api/requests/{wanted['request']['id']}")
        ).json()
        assert not fulfilled_request["can_withdraw"]
        fulfilled_target = fulfilled_request["targets"][0]
        assert fulfilled_target["state"] == "satisfied"
        assert fulfilled_target["can_view_download_history"]
        assert not any(
            fulfilled_target[capability]
            for capability in ("can_recheck", "can_cancel", "can_repair", "needs_review")
        )
        return
    if handoff:
        client = review_account[0]
        pending = (await client.get("/api/acquisition/reviews")).json()["items"][0]
        claimed = await client.post(
            f"/api/acquisition/reviews/{pending['attempt_id']}/claim",
            json={"revision": pending["revision"]},
            headers={"Idempotency-Key": "single-file-member-review"},
        )
        assert claimed.status_code == 202, claimed.text
        await get_queue().run_worker_async(wait=False, concurrency=1)
    async with database() as db:
        attempt = await db.scalar(select(DownloadAttempt))
        assert attempt.inspection_id
    inspected = (await client.get(f"/api/organization/inspections/{attempt.inspection_id}")).json()
    assert inspected["state"] == "ready", inspected
    assert inspected["snapshot"]["source_kind"] == "file"
    assert [file["path"] for file in inspected["snapshot"]["files"]] == [name]
    assert not (await client.get(f"/api/catalog/works/{old['work_id']}")).json()["availability"][
        "owned"
    ]
    settings = (await client.get("/api/organization/settings")).json()
    response = await client.post(
        f"/api/organization/inspections/{inspected['id']}/plans",
        json={
            "inspection_revision": inspected["snapshot"]["revision"],
            "profile_revision": settings["revision"],
            "selections": [
                {
                    "group_key": inspected["snapshot"]["groups"][0]["key"],
                    "work_id": old["work_id"],
                    "version_id": old["version_id"],
                    "full_content": True,
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    if handoff is True:
        async with database() as db, db.begin():
            selection = await db.get(AcquisitionSelection, UUID(selected_response.json()["id"]))
            frozen = selection.frozen
            selection.frozen = {
                **frozen,
                "requirements": {**frozen["requirements"], "version_id": str(uuid4())},
            }
        rejected = await client.post(
            f"/api/organization/inspections/{inspected['id']}/plans",
            json=json.loads(response.request.content),
        )
        assert rejected.status_code == 422, rejected.text
        async with database() as db, db.begin():
            (
                await db.get(AcquisitionSelection, UUID(selected_response.json()["id"]))
            ).frozen = frozen
    route["plan"], route["plan_id"] = response.json(), response.json()["id"]
    assert route["plan"]["document"]["source"]["source_kind"] == "file"
    # Prove a first-time route probe can use the selected file, not only a folder.
    if not handoff:
        await start_probe(client, route, key="file-scope-destination-probe")
        await get_queue().run_worker_async(wait=False, concurrency=1)
    destination = (await client.get("/api/organization/destinations")).json()[0]
    assert destination["probe"]["status"] == "verified", destination
    result = await start_import(client, route, key="file-scoped-import")
    assert result.status_code == 202, result.text
    if handoff:
        other, _ = await admin_client(database, "third-reviewer")
        async with aclosing(other):
            current_review = (await other.get("/api/acquisition/reviews")).json()["items"][0]
            denied = await other.post(
                f"/api/acquisition/reviews/{attempt.id}/claim",
                json={"revision": current_review["revision"]},
                headers={"Idempotency-Key": "cannot-reassign-reserved-import"},
            )
            assert denied.status_code == 409, denied.text
    if handoff in {"grant", "withdraw", "requester", "reviewer"}:
        loop = asyncio.get_running_loop()

        async def revoke():
            async with database() as db, db.begin():
                if handoff == "grant":
                    await db.execute(delete(LibraryGrant))
                elif handoff == "withdraw":
                    for reason in await db.scalars(select(AcquisitionReason)):
                        reason.active = False
                else:
                    identifier = UUID(admin["id"]) if handoff == "requester" else review_account[1]
                    (await db.get(User, identifier)).role = "viewer"

        def checkpoint(phase):
            if phase == "prepared":
                asyncio.run_coroutine_threadsafe(revoke(), loop).result(timeout=10)

        entry = result.json()["entries"][0]
        await execution.execute(UUID(entry["operation_id"]), checkpoint=checkpoint)
        async with database() as db:
            assert (await db.get(ImportEntry, UUID(entry["id"]))).state == "held"
            assert not await db.scalar(select(DownloadFulfillment.id))
        assert not list(route["target"].rglob("*.epub"))
        assert source.read_bytes() == original
        assert qbit.calls.count("submit") == 1
        return
    await get_queue().run_worker_async(wait=False, concurrency=1)
    imported = (await client.get(f"/api/organization/imports/{result.json()['id']}")).json()
    assert imported["entries"][0]["state"] == "confirmed", imported
    async with database() as db:
        fulfillment = await db.scalar(select(DownloadFulfillment))
        assert fulfillment and fulfillment.import_entry_id == UUID(imported["entries"][0]["id"])
        assert fulfillment.evidence["basis"] == "imported"
        selection = await db.get(AcquisitionSelection, UUID(selected_response.json()["id"]))
        assert selection.state == "fulfilled"
        assert (await db.get(AcquisitionReservation, selection.reservation_id)).state == "released"
        assert (await db.scalar(select(DownloadIdentityClaim))).active
    activity = (await owner_client.get(f"/api/acquisition/downloads/{attempt.id}")).json()
    assert activity["fulfillment"]["basis"] == "imported"
    assert activity["fulfillment"]["available_now"]
    output = list(route["target"].rglob("*.mp3" if medium == "audio" else "*.epub"))
    assert len(output) == 1 and output[0].stat().st_ino == source.stat().st_ino
    assert output[0].read_bytes() == source.read_bytes() == original
    assert (source.parent / "unrelated.epub").exists()
    assert (await client.get(f"/api/catalog/works/{old['work_id']}")).json()["availability"][
        "owned"
    ]
    repeated = await start_download(owner_client, selected_response.json())
    assert repeated.json()["id"] == started.json()["id"]
    assert qbit.calls.count("submit") == 1
    assert (await start_import(client, route, key="file-scoped-import-again")).json()["entries"][0][
        "state"
    ] == "skipped"


async def prepare_audio_route(client, database, route, work_id, source):
    catalog = await edition(database, work_id=UUID(work_id), medium="audio")
    async with database() as db, db.begin():
        (await db.get(Version, catalog["version"])).narrators = ["Jordan Lee"]
    inspected = await client.post(
        "/api/organization/inspections",
        headers={"Idempotency-Key": "audio-setup-inspection"},
        json={
            "source_key": "fixture",
            "relative_path": str(source.relative_to(route["source"])),
            "completed_download": True,
        },
    )
    assert inspected.status_code == 202, inspected.text
    await get_queue().run_worker_async(wait=False, concurrency=1)
    snapshot = (await client.get(f"/api/organization/inspections/{inspected.json()['id']}")).json()[
        "snapshot"
    ]
    profile = (await client.get("/api/organization/settings")).json()
    plan = await client.post(
        f"/api/organization/inspections/{inspected.json()['id']}/plans",
        json={
            "inspection_revision": snapshot["revision"],
            "profile_revision": profile["revision"],
            "selections": [
                {
                    "group_key": snapshot["groups"][0]["key"],
                    "work_id": work_id,
                    "version_id": str(catalog["version"]),
                    "full_content": True,
                }
            ],
        },
    )
    assert plan.status_code == 201, plan.text
    changed = await client.put(
        "/api/organization/destinations/ebooks",
        json={
            "library_id": route["library_id"],
            "medium": "audio",
            "backend_path": "/books",
            "expected_revision": route["destination"]["revision"],
        },
    )
    assert changed.status_code == 200, changed.text
    route["destination"], route["plan"], route["plan_id"] = (
        changed.json(),
        plan.json(),
        plan.json()["id"],
    )
    assert (await start_probe(client, route, key="audio-setup-probe")).status_code == 202
    await get_queue().run_worker_async(wait=False, concurrency=1)
    route["destination"] = (await client.get("/api/organization/destinations")).json()[0]
    assert route["destination"]["publication_available"]
    return catalog
