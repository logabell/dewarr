from uuid import UUID

import pytest
from sqlalchemy import select

from app.adapters.catalog_types import BookData, SearchPage
from app.db.models import CatalogAccount, Operation, Work
from app.domain.catalog_refresh import run
from app.security import encrypt_secrets

pytestmark = pytest.mark.integration


async def test_background_match_cancels_when_reader_credentials_change(
    client, admin, database, monkeypatch
):
    (work_id,) = await books(database, admin, "Harbor")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Rotated credentials must not run an old reader lookup")

    monkeypatch.setattr("app.api.metadata.provider_call", forbidden)
    response = await client.post("/api/metadata/reader-matches", json={"work_ids": [str(work_id)]})
    assert response.json()["results"][str(work_id)]["status"] == "pending"
    async with database() as db, db.begin():
        job = await db.scalar(select(Operation).where(Operation.kind == "catalog.refresh"))
        job_id = job.id
        account = await db.get(CatalogAccount, UUID(admin["id"]))
        account.generation += 1
    await run(job_id)
    async with database() as db:
        assert (await db.get(Operation, job_id)).status == "cancelled"


async def books(database, admin, *titles):
    async with database() as db, db.begin():
        db.add(
            CatalogAccount(
                user_id=UUID(admin["id"]),
                encrypted_token=encrypt_secrets({"token": "batch-fixture-token"}),
                generation=1,
                enabled=True,
            )
        )
        works = [Work(title=title, authors=["Writer"], catalog_public=True) for title in titles]
        db.add_all(works)
        await db.flush()
        return [work.id for work in works]


async def test_one_request_resolves_every_visible_card(client, admin, database, monkeypatch):
    first, second = await books(database, admin, "Harbor", "Lighthouse")
    calls = []

    async def call(db, user_id, provider_name, operation, *args):
        calls.append(operation)
        await db.rollback()
        if operation == "search":
            title = args[0].removesuffix(" Writer")
            items = [
                BookData(
                    provider="hardcover",
                    external_id="1" if title == "harbor" else "2",
                    title=title,
                    authors=["Writer"],
                )
            ]
            return (
                SearchPage(provider="hardcover", items=items, page=1, has_more=False),
                False,
                None,
            )
        title = "Harbor" if args[0] == "1" else "Lighthouse"
        return (
            BookData(provider="hardcover", external_id=args[0], title=title, authors=["Writer"]),
            False,
            None,
        )

    monkeypatch.setattr("app.api.metadata.provider_call", call)
    response = await client.post(
        "/api/metadata/reader-matches",
        json={"work_ids": [str(first), str(second), str(UUID(int=7))]},
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert results[str(first)]["status"] == results[str(second)]["status"] == "pending"
    assert not calls
    async with database() as db:
        jobs = list(await db.scalars(select(Operation).where(Operation.kind == "catalog.refresh")))
    assert len(jobs) == 2
    first_job = next(job for job in jobs if job.payload["args"][0] == str(first))
    await run(first_job.id)
    partial = (
        await client.post(
            "/api/metadata/reader-matches", json={"work_ids": [str(first), str(second)]}
        )
    ).json()["results"]
    assert partial[str(first)]["book"]["title"] == "Harbor"
    assert partial[str(second)]["status"] == "pending"
    await run(next(job.id for job in jobs if job.id != first_job.id))
    results = (
        await client.post(
            "/api/metadata/reader-matches",
            json={"work_ids": [str(first), str(second), str(UUID(int=7))]},
        )
    ).json()["results"]
    assert results[str(first)]["book"]["title"] == "Harbor"
    assert results[str(second)]["book"]["title"] == "Lighthouse"
    assert results[str(UUID(int=7))]["book"] is None
    assert calls == ["search", "fetch", "search", "fetch"]


async def test_provider_failure_ends_polling_without_blocking_other_books(
    client, admin, database, monkeypatch
):
    first, second = await books(database, admin, "Harbor", "Lighthouse")
    calls = []

    async def call(db, user_id, provider_name, operation, *args):
        from app.adapters.contracts import AdapterError, FailureKind

        calls.append(operation)
        await db.rollback()
        raise AdapterError(FailureKind.RATE_LIMIT, "Hardcover asked us to wait.")

    monkeypatch.setattr("app.api.metadata.provider_call", call)
    response = await client.post(
        "/api/metadata/reader-matches", json={"work_ids": [str(first), str(second)]}
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert all(value["status"] == "pending" for value in results.values())
    assert not calls
    async with database() as db:
        jobs = list(await db.scalars(select(Operation).where(Operation.kind == "catalog.refresh")))
    await run(jobs[0].id)
    results = (
        await client.post(
            "/api/metadata/reader-matches", json={"work_ids": [str(first), str(second)]}
        )
    ).json()["results"]
    assert results[jobs[0].payload["args"][0]]["reason"] == "Hardcover asked us to wait."
    assert results[jobs[1].payload["args"][0]]["status"] == "pending"
    assert calls == ["search"]


async def test_saving_the_match_just_shown_repeats_no_provider_calls(
    client, admin, database, monkeypatch
):
    (work_id,) = await books(database, admin, "Harbor")
    calls = []

    async def call(db, user_id, provider_name, operation, *args):
        calls.append(operation)
        await db.rollback()
        book = BookData(provider="hardcover", external_id="1", title="Harbor", authors=["Writer"])
        if operation == "search":
            return (
                SearchPage(provider="hardcover", items=[book], page=1, has_more=False),
                False,
                None,
            )
        return book, False, None

    monkeypatch.setattr("app.api.metadata.provider_call", call)
    shown = await client.get(f"/api/metadata/works/{work_id}/reader-match")
    assert shown.json()["status"] == "matched" and calls == ["search", "fetch"]
    saved = await client.post(f"/api/metadata/works/{work_id}/match-hardcover")
    assert saved.status_code == 200, saved.text
    assert saved.json()["book"]["external_id"] == "1"
    assert calls == ["search", "fetch"]
