# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    AcquisitionSelection,
    DownloadAttempt,
    SourceArtifact,
    SourceResult,
    Work,
)
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_book_sources import begin

pytestmark = pytest.mark.integration


async def setup_review(client, database, admin, catalog, selection_route):
    search = await begin(client, catalog, request_id=selection_route["intent_id"])
    async with database() as db, db.begin():
        artifact = await db.get(SourceArtifact, UUID(selection_route["artifact_id"]))
        book = await db.get(Work, catalog["work"])
        raw = {
            **artifact.release_snapshot,
            "title": "Writer collection",
            "raw_title": "Writer collection",
            "description": f"Includes:\n{book.title}\nNot included:\nAbsent book",
        }
        artifact.release_snapshot = raw
        row = SourceResult(
            owner_id=UUID(admin["id"]),
            operation_id=UUID(search["id"]),
            source_key="mam",
            source_generation=1,
            release_snapshot=raw,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            encrypted_reference="test-unused",
        )
        db.add(row)
        await db.flush()
        result_id = str(row.id)
    url = f"/api/source-searches/{search['id']}/results/{result_id}/contents"
    response = await client.get(url, params={"artifact_id": selection_route["artifact_id"]})
    assert response.status_code == 200, response.text
    return url, response.json()


async def test_collection_review_candidates_files_and_frozen_dispatch(
    client, database, admin, catalog, selection_route, monkeypatch
):
    url, review = await setup_review(client, database, admin, catalog, selection_route)
    assert len(review["entries"]) == 1 and len(review["excluded"]) == 1
    entry = review["entries"][0]
    assert entry["candidates"][0]["work_id"] == str(catalog["work"])
    assert review["possible_collection"]
    async with database() as db:
        assert not await db.scalar(select(DownloadAttempt.id))
    assert (await client.get(url, params={"artifact_id": selection_route["artifact_id"]})).json()[
        "review_id"
    ] == review["review_id"]
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    body = {
        "revision": review["revision"],
        "choices": [
            {
                "entry_id": entry["id"],
                "candidate_id": entry["candidates"][0]["id"],
                "paths": [review["files"][0]["path"]],
            }
        ],
    }
    endpoint = f"/api/collection-reviews/{review['review_id']}/download"
    invalid = await client.post(
        endpoint,
        headers={"Idempotency-Key": "bad-collection-choice"},
        json={**body, "choices": [{**body["choices"][0], "paths": ["../wrong.m4b"]}]},
    )
    assert invalid.status_code == 422, invalid.text
    accepted = await client.post(
        endpoint, headers={"Idempotency-Key": "reviewed-collection-download"}, json=body
    )
    assert accepted.status_code == 202, accepted.text
    repeated = await client.post(
        endpoint, headers={"Idempotency-Key": "reviewed-collection-download"}, json=body
    )
    assert repeated.json() == accepted.json()
    async with database() as db:
        selections = list(await db.scalars(select(AcquisitionSelection)))
        assert len(selections) == 1
        assert selections[0].frozen["selected_paths"] == body["choices"][0]["paths"]
        assert selections[0].frozen["collection_review"]["paths"] == body["choices"][0]["paths"]
    missing = await client.post(
        f"/api/collection-reviews/{uuid4()}/download",
        headers={"Idempotency-Key": "unknown-private-review"},
        json=body,
    )
    assert missing.status_code == 404


async def test_alternate_recording_requires_explicit_choice_and_freezes_evidence(
    client, database, admin, catalog, selection_route, monkeypatch
):
    url, _ = await setup_review(client, database, admin, catalog, selection_route)
    async with database() as db, db.begin():
        row = await db.scalar(
            select(SourceResult).where(SourceResult.operation_id == UUID(url.split("/")[3]))
        )
        book = await db.get(Work, catalog["work"])
        row.release_snapshot = {
            **row.release_snapshot,
            "description": (
                f"1980 - {book.title} (read by First Reader)\nAlternate versions:\n"
                f"1980 - {book.title} (read by Second Reader)"
            ),
        }
    review = (await client.get(url, params={"artifact_id": selection_route["artifact_id"]})).json()
    entry = review["entries"][0]
    assert len(entry["recording_options"]) == 2
    body = {
        "revision": review["revision"],
        "choices": [
            {
                "entry_id": entry["id"],
                "candidate_id": entry["candidates"][0]["id"],
                "paths": [review["files"][0]["path"]],
            }
        ],
    }
    endpoint = f"/api/collection-reviews/{review['review_id']}/download"
    rejected = await client.post(
        endpoint, json=body, headers={"Idempotency-Key": "missing-recording"}
    )
    assert rejected.status_code == 422 and "which recording" in rejected.text
    body["choices"][0]["recording_id"] = "0"
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    accepted = await client.post(
        endpoint, json=body, headers={"Idempotency-Key": "chosen-recording"}
    )
    assert accepted.status_code == 202, accepted.text
    async with database() as db:
        selection = await db.scalar(select(AcquisitionSelection))
        assert (
            selection.frozen["collection_review"]["recording"]["narrator_claim"] == "First Reader"
        )


async def test_range_and_files_supplement_partial_description_without_reviving_exclusions(
    client, database, admin, catalog, selection_route, monkeypatch
):
    from app.api import metadata

    async def bibliography(*args):
        return (
            {
                "books": [
                    {
                        "provider": "hardcover",
                        "external_id": "42",
                        "title": "Harbor",
                        "authors": ["Writer"],
                        "aliases": [],
                        "series": [{"name": "Harbor Saga", "position": "1"}],
                    },
                    {
                        "provider": "hardcover",
                        "external_id": "43",
                        "title": "Next Harbor",
                        "authors": ["Writer"],
                        "aliases": [],
                        "series": [{"name": "Harbor Saga", "position": "2"}],
                    },
                    {
                        "provider": "hardcover",
                        "external_id": "44",
                        "title": "Absent book",
                        "authors": ["Writer"],
                        "aliases": [],
                        "series": [{"name": "Harbor Saga", "position": "3"}],
                    },
                    {
                        "provider": "hardcover",
                        "external_id": "45",
                        "title": "Bonus Harbor",
                        "authors": ["Writer"],
                        "aliases": [],
                        "series": [],
                    },
                ],
                "truncated": False,
            },
            False,
            None,
        )

    monkeypatch.setattr(metadata, "provider_call", bibliography)
    url, _ = await setup_review(client, database, admin, catalog, selection_route)
    async with database() as db, db.begin():
        row = await db.scalar(
            select(SourceResult).where(SourceResult.operation_id == UUID(url.split("/")[3]))
        )
        row.release_snapshot = {
            **row.release_snapshot,
            "series": [{"source_id": "9", "name": "Harbor", "position": "1-3"}],
        }
        artifact = await db.get(SourceArtifact, UUID(selection_route["artifact_id"]))
        artifact.descriptor = {
            **artifact.descriptor,
            "files": [
                {"path": "Pack/Harbor.epub", "size_bytes": 100},
                {"path": "Pack/Bonus Harbor.epub", "size_bytes": 100},
                {"path": "Pack/Absent book.epub", "size_bytes": 100},
            ],
        }
    review = (await client.get(url, params={"artifact_id": selection_route["artifact_id"]})).json()
    entries = {e["title"]: e for e in review["entries"]}
    assert set(entries) == {"Harbor", "Next Harbor", "Bonus Harbor"}
    assert any(e["basis"] == "MAM series range" for e in entries["Next Harbor"]["evidence"])
    assert entries["Harbor"]["files"] == ["Pack/Harbor.epub"]
    assert entries["Bonus Harbor"]["files"] == ["Pack/Bonus Harbor.epub"]
    assert entries["Bonus Harbor"]["match"] == "review"
