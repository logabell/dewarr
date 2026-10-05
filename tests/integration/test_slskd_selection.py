"""Soulseek selection must respect the requesting reader's accepted scope."""

# ruff: noqa: F401, F811
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from app.adapters.slskd import group_responses
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    DownloadAttempt,
    Integration,
    Operation,
    SourceArtifact,
    SourceConnection,
    SourceResult,
)
from app.domain import acquisition, automatic_selection, slskd_transfers
from app.domain.automatic_selection import resolve_candidate
from app.domain.slskd_transfers import queue_folder as queue_soulseek_folder
from app.security import encrypt_secrets
from tests.integration.test_acquisition import body, catalog, request
from tests.integration.test_acquisition_defaults import save
from tests.integration.test_acquisition_selections import prepare, selection_route
from tests.integration.test_automatic_dispatch import authorized
from tests.integration.test_automatic_selection import source

pytestmark = pytest.mark.integration


@pytest.fixture
async def soulseek(client, database, authorized, monkeypatch):
    release = group_responses(
        [{"username": "reader", "files": [{"filename": r"Writer\Harbor\Harbor.m4b", "size": 12}]}],
        search_id="slskd-search",
        observed_at=datetime.now(UTC),
        title="Harbor",
        authors=["Writer"],
    )[0]
    async with database() as db, db.begin():
        downloader = await db.get(Integration, UUID(authorized["body"]["downloader_id"]))
        downloader.kind = "slskd"
        downloader.encrypted_secrets = encrypt_secrets({"api_key": "fixture-slskd-key"})
        db.add(
            SourceConnection(
                key="slskd",
                base_url="http://slskd.test",
                encrypted_secrets=downloader.encrypted_secrets,
            )
        )
        result = await db.get(SourceResult, authorized["result"])
        result.source_key = "slskd"
        result.release_snapshot = release.model_dump(mode="json")
        result.encrypted_reference = encrypt_secrets({})
    # The shared MAM fixture stubs resolution. Exercise Soulseek's real resolver
    # here, including file-list persistence, and mock only the network handoff.
    monkeypatch.setattr(automatic_selection, "resolve_candidate", resolve_candidate)
    enqueue = AsyncMock()
    monkeypatch.setattr(slskd_transfers, "queue_folder", enqueue)
    return {**authorized, "enqueue": enqueue}


async def any_language_search(client, database, catalog, soulseek, *, bound=None):
    await save(
        client,
        {
            "desired_media": "audio",
            "language": None,
            "audio_library_id": str(catalog["library"]),
            "audio_destination_id": soulseek["body"]["destination_id"],
            "source_order": ["slskd"],
        },
    )
    profiles = (await client.get("/api/acquisition/profiles")).json()
    async with database() as db, db.begin():
        search = await db.get(Operation, soulseek["search"])
        search.payload = {
            **search.payload,
            "profile": profiles[0],
            "command": {"request_id": bound},
        }
    return f"/api/source-searches/{soulseek['search']}/results/{soulseek['result']}/download"


@pytest.mark.parametrize("change", ["disabled", "credentials", "endpoint", "deleted"])
async def test_unaccepted_soulseek_batch_cleans_up_only_the_same_client_after_disable(
    client, database, catalog, soulseek, monkeypatch, change
):
    queued = []
    cancelled = []
    endpoints = []

    class SoulseekClient:
        def __init__(self, endpoint, api_key):
            endpoints.append(endpoint)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def enqueue(self, release, *, attempt_id):
            queued.append((release.username, attempt_id))
            # Settings can change while the remote enqueue is in flight, before
            # prepare obtains the source/settings locks and accepts this batch.
            async with database() as db, db.begin():
                source = await db.get(SourceConnection, "slskd")
                source.enabled = False
                downloader = await db.get(Integration, UUID(soulseek["body"]["downloader_id"]))
                downloader.enabled = False
                if change == "credentials":
                    downloader.credential_generation += 1
                    downloader.encrypted_secrets = encrypt_secrets({"api_key": "replacement"})
                elif change == "endpoint":
                    downloader.base_url = "http://replacement.test"
                elif change == "deleted":
                    downloader.deleted_at = datetime.now(UTC)

        async def cancel(self, username, attempt_id):
            cancelled.append((username, attempt_id))

    monkeypatch.setattr(slskd_transfers, "SlskdClient", SoulseekClient)
    monkeypatch.setattr(slskd_transfers, "queue_folder", queue_soulseek_folder)
    url = await any_language_search(client, database, catalog, soulseek)
    response = await client.post(url, headers={"Idempotency-Key": "soulseek-disabled-cleanup"})
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    await automatic_selection.run(identifier)
    async with database() as db:
        operation = await db.get(Operation, identifier)
        assert operation.status == "held", operation.message
        assert "source connection changed" in operation.message
        assert not await db.scalar(select(DownloadAttempt.id))
    assert len(queued) == 1
    assert cancelled == (queued if change == "disabled" else [])
    assert len(endpoints) == (2 if change == "disabled" else 1)
    if change == "disabled":
        assert endpoints[0] == endpoints[1]


@pytest.mark.parametrize("older_language", [None, "en", "edition"])
async def test_any_language_soulseek_selection_does_not_inherit_another_requests_language(
    client, database, catalog, soulseek, older_language
):
    if older_language:
        constraints = (
            {"audio_version_id": str(catalog["versions"][1])}
            if older_language == "edition"
            else {"language": older_language}
        )
        old = await request(
            client, body(catalog, "audio", audio_library_id=str(catalog["library"]), **constraints)
        )
    url = await any_language_search(client, database, catalog, soulseek)
    response = await client.post(url, headers={"Idempotency-Key": "soulseek-any-language"})
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    # Reconciliation must not reattach the now independent request to the
    # older reservation before the worker validates and dispatches the release.
    await acquisition.reconcile_requests()
    await automatic_selection.run(identifier)
    await acquisition.reconcile_requests()
    await automatic_selection.run(identifier)
    repeated = await client.post(url, headers={"Idempotency-Key": "soulseek-any-language"})
    assert repeated.status_code == 202 and repeated.json()["id"] == str(identifier)
    async with database() as db:
        operation = await db.get(Operation, identifier)
        intent = await db.get(AcquisitionIntent, UUID(operation.payload["command"]["intent_id"]))
        assert intent.specification["language"] is None
        assert operation.status == "completed", (
            operation.message,
            operation.payload.get("decisions"),
        )
        assert operation.payload["requirements"]["language"] is None
        selected = await db.get(AcquisitionSelection, UUID(operation.payload["selection_id"]))
        artifact = await db.get(SourceArtifact, selected.artifact_id)
        assert artifact.source_key == "slskd"
        assert artifact.descriptor["parser"] == "slskd-file-list"
        assert selected.frozen["requirements"]["language"] is None
        assert selected.frozen["requirements"]["version_id"] is None
        if older_language:
            older = await db.get(AcquisitionIntent, UUID(old["request"]["id"]))
            target = await db.scalar(
                select(AcquisitionTarget).where(AcquisitionTarget.intent_id == older.id)
            )
            reservation = await db.get(AcquisitionReservation, target.reservation_id)
            assert reservation.id != selected.reservation_id
            assert reservation.requirements == acquisition.RequestSpec.model_validate(
                older.specification
            ).rule("audio")
            assert reservation.state == "planned"
    assert soulseek["enqueue"].await_count == 1


@pytest.mark.parametrize("requirement", ["language", "edition"])
async def test_bound_soulseek_request_keeps_its_own_language_after_defaults_change(
    client, database, catalog, soulseek, requirement
):
    await save(client, {"language": "en"})
    constraints = (
        {"audio_version_id": str(catalog["versions"][1]), "language": None}
        if requirement == "edition"
        else {}
    )
    old = await request(
        client, body(catalog, "audio", audio_library_id=str(catalog["library"]), **constraints)
    )
    url = await any_language_search(client, database, catalog, soulseek, bound=old["request"]["id"])
    response = await client.post(url, headers={"Idempotency-Key": "soulseek-strict-language"})
    assert response.status_code == 202, response.text
    identifier = UUID(response.json()["id"])
    await automatic_selection.run(identifier)
    async with database() as db:
        operation = await db.get(Operation, identifier)
        assert operation.status == "held"
        assert "required language" in operation.message
        assert not await db.scalar(select(DownloadAttempt.id))
        intent = await db.get(AcquisitionIntent, UUID(old["request"]["id"]))
        assert intent.specification == old["request"]["specification"]
    soulseek["enqueue"].assert_not_awaited()


@pytest.mark.parametrize("other_language", ["en", "fr"])
async def test_reviewed_release_shares_only_when_both_requests_accept_its_language(
    client, database, catalog, selection_route, other_language
):
    old = await request(
        client,
        body(catalog, "audio", language=other_language, audio_library_id=str(catalog["library"])),
    )
    response = await prepare(client, selection_route)
    assert response.status_code == 201, response.text
    await acquisition.reconcile_requests()
    async with database() as db:
        selected = await db.get(AcquisitionSelection, UUID(response.json()["id"]))
        other = await db.scalar(
            select(AcquisitionTarget).where(
                AcquisitionTarget.intent_id == UUID(old["request"]["id"])
            )
        )
        reservation = await db.get(AcquisitionReservation, other.reservation_id)
        assert reservation.requirements["language"] == other_language
        assert (selected.reservation_id == reservation.id) is (other_language == "en")
        assert selected.frozen["requirements"]["language"] == (
            "en" if other_language == "en" else None
        )
