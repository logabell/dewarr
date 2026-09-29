# ruff: noqa: F401, F811
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.adapters.mam import ReleasePage
from app.db.models import (
    AcquisitionReason,
    AcquisitionSelection,
    AutomaticImportPolicy,
    CatalogSeries,
    DownloadAttempt,
    DownloadMembership,
    Operation,
    SeriesMembership,
    User,
    Work,
    WorkMetadataSource,
)
from app.domain import (
    automatic_packs,
    automatic_selection,
    book_sources,
    download_attempts,
    list_automation,
    series_acquisition,
    series_requests,
)
from tests.integration.test_acquisition import catalog
from tests.integration.test_acquisition_selections import selection_route
from tests.integration.test_automatic_dispatch import authorized
from tests.integration.test_automatic_pack_selection import series_pack
from tests.integration.test_automatic_selection import source

pytestmark = pytest.mark.integration
BASE = "/api/catalog/series/hardcover/pack-series/requests"


@pytest.fixture
async def ready(client, database, admin, series_pack, authorized, source, monkeypatch):
    async with database() as db, db.begin():
        works = list(
            await db.scalars(
                select(SeriesMembership.work_id)
                .where(SeriesMembership.series_id == series_pack)
                .order_by(SeriesMembership.work_id)
            )
        )
        for index, work_id in enumerate(works):
            if not await db.scalar(
                select(WorkMetadataSource.id).where(WorkMetadataSource.work_id == work_id)
            ):
                db.add(
                    WorkMetadataSource(
                        work_id=work_id,
                        provider="hardcover",
                        external_id=str(200 + index),
                        accepted=True,
                        fetched_at=datetime.now(UTC),
                        snapshot={"title": "Roads", "authors": ["Writer"]},
                    )
                )

    async def search(*args, **kwargs):
        return ReleasePage(
            items=[source["release"]], offset=0, limit=50, total=1, has_more=False
        ), 1

    monkeypatch.setattr(book_sources, "source_call", search)
    route = authorized["body"]
    command = {
        "work_ids": list(map(str, works)),
        "expected_generation": 1,
        "scope": "complete_series",
        "confirm_main_membership": True,
        "specification": {"mode": "audio"},
        "automatic": {
            "downloader_id": route["downloader_id"],
            "downloader_generation": 1,
            "routes": {
                "audio": {
                    "destination_id": route["destination_id"],
                    "destination_revision": route["destination_revision"],
                }
            },
        },
    }
    response = await client.post(
        BASE + "/preview", json=command, headers={"Idempotency-Key": "series-automatic-preview"}
    )
    assert response.status_code == 201, response.text
    return {
        "parent": UUID(response.json()["id"]),
        "command": command,
        "works": works,
        "qbit": authorized["qbit"],
    }


async def accept(client, database, ready):
    response = await client.post(f"{BASE}/{ready['parent']}/submit")
    assert response.status_code == 202, response.text
    await series_requests.run(ready["parent"])
    async with database() as db:
        return UUID((await db.get(Operation, ready["parent"])).payload["acquisition_id"])


async def tick(database, identifier):
    async with database() as db, db.begin():
        row = await db.get(Operation, identifier)
        payload = deepcopy(row.payload)
        for book in payload["books"].values():
            if book["next_at"]:
                book["next_at"] = datetime.now(UTC).isoformat()
        row.payload = payload
    await series_acquisition.run(identifier)


async def prepared(client, database, ready):
    identifier = await accept(client, database, ready)
    await tick(database, identifier)
    async with database() as db:
        row = await db.get(Operation, identifier)
        searches = [
            UUID(b["progress"]["audio"]["search_id"]) for b in row.payload["books"].values()
        ]
    for search in searches:
        await book_sources.run(search, "mam")
    await tick(database, identifier)
    async with database() as db:
        row = await db.get(Operation, identifier)
        selections = [
            UUID(b["progress"]["audio"]["selection_id"]) for b in row.payload["books"].values()
        ]
    return identifier, selections


async def test_preview_has_no_dispatch_and_replay_creates_one_finite_controller(
    client, database, ready
):
    async with database() as db:
        assert not await db.scalar(
            select(Operation.id).where(Operation.kind == series_acquisition.KIND)
        )
        assert not await db.scalar(select(DownloadAttempt.id))
    identifier = await accept(client, database, ready)
    await series_requests.run(ready["parent"])
    await asyncio.gather(tick(database, identifier), tick(database, identifier))
    async with database() as db:
        row = await db.get(Operation, identifier)
        assert len(row.payload["books"]) == 2
        assert {r["state"] for r in row.payload["books"].values()} == {"searching"}
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.kind == series_acquisition.KIND)
            )
            == 1
        )
    assert not ready["qbit"].calls


async def test_series_cancellation_fences_already_prepared_pack_without_submitting(
    client, database, ready
):
    identifier, selections = await prepared(client, database, ready)
    for selection in selections:
        await automatic_selection.run(selection)
    await automatic_packs.run(selections[0])
    async with database() as db:
        attempt = await db.scalar(select(DownloadAttempt))
        assert attempt is not None
        assert await db.scalar(select(func.count()).select_from(DownloadMembership)) == 2
    response = await client.post(f"{BASE}/{ready['parent']}/cancel")
    assert response.status_code == 200, response.text
    await download_attempts.run(attempt.id)
    await series_acquisition.run(identifier)
    async with database() as db:
        assert (await db.get(Operation, identifier)).status == "cancelled"
        assert not (await db.get(DownloadAttempt, attempt.id)).external_may_exist
        assert not await db.scalar(
            select(AcquisitionReason.id).where(
                AcquisitionReason.kind == "series", AcquisitionReason.active.is_(True)
            )
        )
        assert await db.scalar(
            select(AcquisitionReason.id).where(
                AcquisitionReason.kind == "manual", AcquisitionReason.active.is_(True)
            )
        )
    assert "submit" not in ready["qbit"].calls


async def test_changed_route_approval_invalidates_preview_acceptance(client, database, ready):
    async with database() as db, db.begin():
        (await db.scalar(select(AutomaticImportPolicy))).generation += 1
    response = await client.post(f"{BASE}/{ready['parent']}/submit")
    assert response.status_code == 409, response.text
    async with database() as db:
        assert not await db.scalar(
            select(Operation.id).where(Operation.kind == series_acquisition.KIND)
        )


async def test_cancellation_during_artifact_lookup_revokes_selection(
    client, database, ready, source
):
    _, selections = await prepared(client, database, ready)

    async def cancel():
        response = await client.post(f"{BASE}/{ready['parent']}/cancel")
        assert response.status_code == 200, response.text

    source["resolver"].callback = cancel
    await automatic_selection.run(selections[0])
    async with database() as db:
        operation = await db.get(Operation, selections[0])
        assert operation.status == "held", operation.message
        assert not await db.scalar(select(AcquisitionSelection.id))
        assert not await db.scalar(select(DownloadAttempt.id))


async def test_one_book_failure_rolls_back_its_progress_without_stranding_other_book(
    client, database, ready, monkeypatch
):
    identifier = await accept(client, database, ready)
    original = list_automation.advance_target

    async def fail_one(db, user, policy, book, *args, **kwargs):
        result = await original(db, user, policy, book, *args, **kwargs)
        if book.work_id == ready["works"][0]:
            raise HTTPException(409, "Fixture route changed after queued search")
        return result

    monkeypatch.setattr(list_automation, "advance_target", fail_one)
    await tick(database, identifier)
    async with database() as db, db.begin():
        row = await db.get(Operation, identifier)
        first, second = [row.payload["books"][str(w)] for w in ready["works"]]
        assert first["state"] == "held" and first["progress"] == {}, first
        assert second["state"] == "searching" and second["progress"]["audio"]["search_id"]
        assert row.status == "running"
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status='succeeded' WHERE id=:id"),
            {"id": row.job_id},
        )
        revision = row.payload["scope_revision"]
    current = (await client.get(f"{BASE}/{ready['parent']}")).json()
    assert current["can_retry_acquisition"] and current["acquisition_status"] == "running"
    response = await client.post(f"{BASE}/{ready['parent']}/retry-acquisition")
    assert response.status_code == 202, response.text
    monkeypatch.setattr(list_automation, "advance_target", original)
    await tick(database, identifier)
    async with database() as db:
        row = await db.get(Operation, identifier)
        assert row.payload["scope_revision"] == revision
        assert {book["state"] for book in row.payload["books"].values()} == {"searching"}
        assert (
            row.payload["books"][str(ready["works"][1])]["progress"]["audio"]["search_id"]
            == second["progress"]["audio"]["search_id"]
        )


async def test_stopped_worker_is_recoverable_without_new_scope(client, database, ready):
    identifier = await accept(client, database, ready)
    async with database() as db, db.begin():
        row = await db.get(Operation, identifier)
        before = row.payload["scope_revision"]
        await db.execute(
            text("UPDATE book_queue.procrastinate_jobs SET status='failed' WHERE id=:id"),
            {"id": row.job_id},
        )
    await series_acquisition.schedule()
    current = (await client.get(f"{BASE}/{ready['parent']}")).json()
    assert current["can_retry_acquisition"] and current["acquisition_status"] == "held", current
    response = await client.post(f"{BASE}/{ready['parent']}/retry-acquisition")
    assert response.status_code == 202, response.text
    async with database() as db:
        row = await db.get(Operation, identifier)
        assert row.payload["scope_revision"] == before and row.status == "queued"
        assert len(row.payload["books"]) == 2


@pytest.mark.parametrize("change", ["permission", "series_reason", "identity"])
async def test_changed_authority_before_dispatch_does_not_borrow_other_consent(
    client, database, ready, change
):
    _, selections = await prepared(client, database, ready)
    for selection in selections:
        await automatic_selection.run(selection)
    await automatic_packs.run(selections[0])
    async with database() as db, db.begin():
        parent = await db.get(Operation, ready["parent"])
        attempt = await db.scalar(select(DownloadAttempt))
        assert attempt is not None
        if change == "permission":
            owner = await db.get(User, parent.owner_id)
            owner.role, owner.can_automate = "member", False
        elif change == "series_reason":
            reasons = await db.scalars(
                select(AcquisitionReason).where(
                    AcquisitionReason.reference == str(parent.id),
                    AcquisitionReason.kind == "series",
                )
            )
            for reason in reasons:
                reason.active = False
        else:
            for work_id in ready["works"]:
                (await db.get(Work, work_id)).title = "A different book identity"
    await download_attempts.run(attempt.id)
    async with database() as db:
        current = await db.get(DownloadAttempt, attempt.id)
        assert not current.external_may_exist
        assert current.state == "held"
    assert "submit" not in ready["qbit"].calls


async def test_catalog_refresh_cannot_expand_an_accepted_series_set(
    client, database, ready, series_pack
):
    identifier = await accept(client, database, ready)
    async with database() as db, db.begin():
        row = await db.get(Operation, identifier)
        revision = row.payload["scope_revision"]
        series = await db.get(CatalogSeries, series_pack)
        series.generation += 1
        new_work = Work(title="Unrequested third book", authors=["Writer"])
        db.add(new_work)
        await db.flush()
        member = await db.scalar(
            select(SeriesMembership).where(SeriesMembership.series_id == series_pack).limit(1)
        )
        db.add(
            SeriesMembership(
                series_id=series_pack,
                work_id=new_work.id,
                external_id="new-after-acceptance",
                snapshot={**member.snapshot, "position": "3"},
            )
        )
    await tick(database, identifier)
    async with database() as db:
        row = await db.get(Operation, identifier)
        assert row.payload["scope_revision"] == revision
        assert set(row.payload["books"]) == set(map(str, ready["works"]))
        assert {book["state"] for book in row.payload["books"].values()} == {"searching"}


async def test_selected_series_books_share_one_eligible_mam_pack_transfer(client, database, ready):
    identifier, selections = await prepared(client, database, ready)
    for selection in selections:
        await automatic_selection.run(selection)
    await automatic_packs.run(selections[0])
    async with database() as db:
        attempt = await db.scalar(select(DownloadAttempt))
        assert attempt is not None
        assert await db.scalar(select(func.count()).select_from(DownloadMembership)) == 2
    await download_attempts.run(attempt.id)
    await download_attempts.run(attempt.id)
    await series_acquisition.run(identifier)
    async with database() as db:
        assert (await db.get(DownloadAttempt, attempt.id)).external_may_exist
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
    assert ready["qbit"].calls.count("submit") == 1
