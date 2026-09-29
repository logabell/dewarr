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
        artifact = await db.get(SourceArtifact, UUID(selection_route["artifact_id"]))
        artifact.descriptor = {
            **artifact.descriptor,
            "files": [
                {
                    "index": i,
                    "path": f"{artifact.descriptor['name']}/{book.title}/{reader}.m4b",
                    "size_bytes": 100,
                }
                for i, reader in enumerate(("First Reader", "Second Reader"))
            ],
        }
    review = (await client.get(url, params={"artifact_id": selection_route["artifact_id"]})).json()
    entry = review["entries"][0]
    assert len(entry["recording_options"]) == 2
    assert entry["suggested_recording_id"] == "0"
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
    assert set(entries) == {"Harbor", "Bonus Harbor"}
    assert any(e["basis"] == "MAM series range" for e in entries["Harbor"]["evidence"])
    assert entries["Harbor"]["files"] == ["Pack/Harbor.epub"]
    assert entries["Bonus Harbor"]["files"] == ["Pack/Bonus Harbor.epub"]
    assert entries["Bonus Harbor"]["suggested_candidate_id"] == "hardcover:45"


async def test_file_backed_matches_collapse_aliases_and_allow_catalog_correction(
    client, database, admin, catalog, selection_route, monkeypatch
):
    from app.api import metadata

    books = [
        {"external_id": "42", "title": "Harbor", "users_count": 100},
        {"external_id": "43", "title": "Harbor", "users_count": 1},
        {"external_id": "44", "title": "The Harbor Saga", "aliases": ["Harbor"]},
        {"external_id": "45", "title": "The Next Harbor"},
        {"external_id": "46", "title": "Corrected Book"},
        {
            "external_id": "47",
            "title": "The Short Tale of Harbor",
            "cover_url": "https://assets.hardcover.app/novella.jpg",
        },
        {"external_id": "48", "title": "The Short Tale of Harbor An Eclipse Novella"},
        {"external_id": "49", "title": "Eclipse"},
    ]
    for book in books:
        book.update(provider="hardcover", authors=["Writer"])
        book.setdefault("series", [{"external_id": "9", "name": "Harbor Saga", "position": "1"}])

    async def bibliography(*args):
        return {"books": books, "truncated": False}, False, None

    monkeypatch.setattr(metadata, "provider_call", bibliography)
    url, _ = await setup_review(client, database, admin, catalog, selection_route)
    async with database() as db, db.begin():
        artifact = await db.get(SourceArtifact, UUID(selection_route["artifact_id"]))
        artifact.descriptor = {
            **artifact.descriptor,
            "name": "Pack",
            "files": [
                {"index": 0, "path": "Pack/The Harbor Saga/Harbor.epub", "size_bytes": 100},
                {"index": 1, "path": "Pack/The Harbor Saga/Next Harbor.epub", "size_bytes": 100},
                {
                    "index": 2,
                    "path": "Pack/Short Tale of Harbor An Eclipse Novella.epub",
                    "size_bytes": 100,
                },
            ],
        }
    response = await client.get(url, params={"artifact_id": selection_route["artifact_id"]})
    assert response.status_code == 200, response.text
    review = response.json()
    assert len(review["entries"]) == 3
    assert review["entries"][1]["title"] == "The Next Harbor"
    first, second, novella = review["entries"]
    assert novella["suggested_candidate_id"] == "hardcover:47"
    assert novella["files"] == ["Pack/Short Tale of Harbor An Eclipse Novella.epub"]
    assert first["title"] == "Harbor"
    assert first["suggested_candidate_id"] == "hardcover:42"
    assert first["files"] == ["Pack/The Harbor Saga/Harbor.epub"]
    assert {c["id"] for c in first["candidates"]} == {
        "hardcover:42",
        "hardcover:43",
        "hardcover:44",
    }
    assert second["suggested_candidate_id"] == "hardcover:45"
    assert second["files"] == ["Pack/The Harbor Saga/Next Harbor.epub"]
    assert any(c["id"] == "hardcover:46" for c in review["catalog_candidates"])
    assert not review["warnings"]
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    body = {
        "revision": review["revision"],
        "choices": [
            {
                "entry_id": first["id"],
                "candidate_id": "hardcover:46",
                "paths": first["files"],
            }
        ],
    }
    endpoint = f"/api/collection-reviews/{review['review_id']}/download"
    invalid = await client.post(
        endpoint,
        headers={"Idempotency-Key": "unknown-correction"},
        json={**body, "choices": [{**body["choices"][0], "candidate_id": "hardcover:999"}]},
    )
    assert invalid.status_code == 422
    corrected = await client.post(
        endpoint, headers={"Idempotency-Key": "known-correction"}, json=body
    )
    assert corrected.status_code == 202, corrected.text
    async with database() as db:
        selection = await db.scalar(select(AcquisitionSelection))
        assert selection.frozen["selected_paths"] == first["files"]


@pytest.mark.parametrize("rejected_after_preview", [False, True])
async def test_series_observation_seeds_collection_catalog_before_metadata_is_imported(
    client, database, admin, catalog, selection_route, monkeypatch, rejected_after_preview
):
    from sqlalchemy import delete

    from app.api import metadata
    from app.db.models import CatalogSeries, ProviderObject, SeriesMembership, WorkMetadataSource

    url, _ = await setup_review(client, database, admin, catalog, selection_route)
    cover = "https://assets.hardcover.app/book-cover.jpg"
    async with database() as db, db.begin():
        book = await db.get(Work, catalog["work"])
        title = book.title
        await db.execute(
            delete(ProviderObject).where(
                ProviderObject.metadata_source_id.in_(
                    select(WorkMetadataSource.id).where(WorkMetadataSource.work_id == book.id)
                )
            )
        )
        await db.execute(delete(WorkMetadataSource).where(WorkMetadataSource.work_id == book.id))
        series = CatalogSeries(
            owner_id=UUID(admin["id"]),
            provider="hardcover",
            external_id="808",
            name="Harbor Saga",
            fetched_at=datetime.now(UTC),
        )
        db.add(series)
        await db.flush()
        db.add(
            SeriesMembership(
                series_id=series.id,
                external_id="42",
                work_id=book.id,
                present=True,
                snapshot={
                    "position": "1",
                    "book": {
                        "provider": "hardcover",
                        "external_id": "42",
                        "title": title,
                        "authors": book.authors,
                        "cover_url": cover,
                        "description": "The first Harbor adventure.",
                    },
                },
            )
        )

    search = await begin(client, catalog, key="series-observation-search")
    async with database() as db, db.begin():
        row = await db.get(SourceResult, UUID(url.split("/")[5]))
        row.operation_id = UUID(search["id"])
    url = f"/api/source-searches/{search['id']}/results/{url.split('/')[5]}/contents"
    calls = []

    async def bibliography(db, owner, provider, method, external_id):
        calls.append((provider, method, external_id))
        return {"books": [], "truncated": False}, False, None

    monkeypatch.setattr(metadata, "provider_call", bibliography)
    response = await client.get(url, params={"artifact_id": selection_route["artifact_id"]})
    assert response.status_code == 200, response.text
    review = response.json()
    assert calls == [("hardcover", "collection_bibliography", "42")]
    candidate = next(c for c in review["catalog_candidates"] if c["title"] == title)
    assert candidate["cover_url"] == cover
    assert candidate["series"] == [{"external_id": "808", "name": "Harbor Saga", "position": "1"}]
    async with database() as db:
        assert (await db.get(Work, catalog["work"])).cover_url != cover
        assert not await db.scalar(
            select(WorkMetadataSource.id).where(WorkMetadataSource.work_id == catalog["work"])
        )
    monkeypatch.setattr(get_settings(), "download_dispatch_enabled", True)
    if rejected_after_preview:
        async with database() as db, db.begin():
            db.add(
                WorkMetadataSource(
                    work_id=catalog["work"],
                    provider="hardcover",
                    external_id="42",
                    accepted=False,
                    snapshot={},
                    fetched_at=datetime.now(UTC),
                )
            )
    entry = next(
        e for e in review["entries"] if candidate["id"] in {c["id"] for c in e["candidates"]}
    )
    accepted = await client.post(
        f"/api/collection-reviews/{review['review_id']}/download",
        headers={"Idempotency-Key": "confirm-series-metadata"},
        json={
            "revision": review["revision"],
            "choices": [
                {
                    "entry_id": entry["id"],
                    "candidate_id": candidate["id"],
                    "paths": [review["files"][0]["path"]],
                }
            ],
        },
    )
    if rejected_after_preview:
        assert accepted.status_code == 409, accepted.text
        async with database() as db:
            assert not await db.scalar(select(DownloadAttempt.id))
            assert (await db.get(Work, catalog["work"])).cover_url != cover
        return
    assert accepted.status_code == 202, accepted.text
    async with database() as db:
        saved = await db.get(Work, catalog["work"])
        assert saved.cover_url == cover
        assert saved.description == "The first Harbor adventure."
        assert await db.scalar(
            select(WorkMetadataSource.accepted).where(
                WorkMetadataSource.work_id == saved.id, WorkMetadataSource.external_id == "42"
            )
        )
    requests = (await client.get("/api/requests")).json()["items"]
    assert next(r for r in requests if r["work_id"] == str(catalog["work"]))["cover_url"] == cover
