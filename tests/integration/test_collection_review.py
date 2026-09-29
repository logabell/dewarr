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
