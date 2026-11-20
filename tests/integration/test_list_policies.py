# ruff: noqa: F811
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.adapters.mam import ReleasePage
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionSelection,
    DownloadAttempt,
    ListAcquisitionBook,
    ListAcquisitionPolicy,
    Operation,
    User,
    Work,
)
from app.domain import book_sources, list_automation
from app.jobs.queue import get_queue
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_automatic_dispatch import authorized  # noqa: F401
from tests.integration.test_automatic_selection import source  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def policy_fixture(client, admin, catalog, authorized, monkeypatch):
    shelf = (await client.post("/api/lists", json={"name": "Automatic shelf"})).json()["id"]
    route = authorized["body"]
    config = {
        "mode": "automatic",
        "specification": {"mode": "audio"},
        "downloader_id": route["downloader_id"],
        "downloader_generation": route["downloader_generation"],
        "routes": {
            "audio": {
                "destination_id": route["destination_id"],
                "destination_revision": route["destination_revision"],
            }
        },
    }
    calls = []

    async def search(owner, action, value, **kwargs):
        calls.append(action)
        return ReleasePage(
            items=[authorized["release"]], offset=0, limit=50, total=1, has_more=False
        ), 1

    monkeypatch.setattr(book_sources, "source_call", search)
    return {
        "list": shelf,
        "config": config,
        "work": str(catalog["work"]),
        "source": authorized,
        "calls": calls,
    }


async def preview(client, fixture, **changes):
    response = await client.post(
        f"/api/lists/{fixture['list']}/acquisition/preview",
        json={**fixture["config"], **changes},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def activate(client, fixture, plan):
    response = await client.post(
        f"/api/lists/{fixture['list']}/acquisition/previews/{plan['id']}/activate"
    )
    assert response.status_code == 200, response.text
    return response.json()


async def add(client, fixture, work=None):
    response = await client.post(
        f"/api/lists/{fixture['list']}/entries", json={"work_id": work or fixture["work"]}
    )
    assert response.status_code == 204


async def tick(database, policy, *, worker=True, force_books=False):
    async with database() as db, db.begin():
        row = await db.get(ListAcquisitionPolicy, UUID(policy["id"]))
        row.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
        if force_books:
            for book in await db.scalars(
                select(ListAcquisitionBook).where(
                    ListAcquisitionBook.policy_id == row.id,
                    ListAcquisitionBook.next_check_at.is_not(None),
                )
            ):
                book.next_check_at = datetime.now(UTC) - timedelta(seconds=1)
    await list_automation.schedule()
    if worker:
        await get_queue().run_worker_async(wait=False, concurrency=1)
    else:
        async with database() as db:
            row = await db.get(ListAcquisitionPolicy, UUID(policy["id"]))
        await list_automation.run(row.operation_id)


async def test_future_only_activation_skips_backlog_and_new_addition_downloads_once(
    client, database, policy_fixture
):
    f = policy_fixture
    # A different existing book demonstrates that activation does not consume the backlog.
    async with database() as db, db.begin():
        old = Work(title="Earlier member", authors=["Writer"])
        db.add(old)
        await db.flush()
        old_id = str(old.id)
    await add(client, f, old_id)
    plan = await preview(client, f)
    assert plan["total"] == 1 and plan["selected"] == 0
    saved = await activate(client, f, plan)
    await tick(database, saved)
    assert f["calls"] == []
    await add(client, f)
    await tick(database, saved)
    await tick(database, saved, force_books=True)
    await tick(database, saved, force_books=True)
    assert f["source"]["qbit"].calls.count("submit") == 1
    async with database() as db:
        attempts = list(await db.scalars(select(DownloadAttempt)))
        assert len(attempts) == 1
        selection = await db.get(AcquisitionSelection, attempts[0].selection_id)
        proof = selection.frozen["automatic_selection"]["list_authority"]
        assert proof["policy_id"] == saved["id"]
        book = await db.get(ListAcquisitionBook, UUID(proof["book_id"]))
        assert book.intent_id == selection.intent_id
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AcquisitionIntent)
                .where(AcquisitionIntent.work_id == UUID(old_id))
            )
            == 0
        )
    assert (await activate(client, f, plan))["revision"] == saved["revision"]


async def test_select_all_missing_backlog_is_frozen_and_activated(
    client, database, policy_fixture
):
    f = policy_fixture
    await add(client, f)

    # Read the current membership revision using the same list endpoint as the picker.
    listing = await client.get(f"/api/lists/{f['list']}?offset=0&limit=25")
    assert listing.status_code == 200, listing.text
    revision = listing.json()["content_revision"]

    # The shared fixture creates an active request. Withdraw its reason so
    # this test exercises a genuinely missing book, not a pending reservation.
    request_id = f["source"]["body"]["intent_id"]
    current = await client.get(f"/api/requests/{request_id}")
    assert current.status_code == 200, current.text
    reason = current.json()["reasons"][0]
    withdrawn = await client.delete(
        f"/api/requests/{request_id}/reasons/{reason['id']}"
    )
    assert withdrawn.status_code == 200, withdrawn.text

    plan = await preview(
        client, f, select_all_missing=True, expected_content_revision=revision
    )
    assert plan["selected"] == 1

    saved = await activate(client, f, plan)

    selected = await client.get(
        f"/api/lists/{f['list']}/acquisition/selected?offset=0&limit=25"
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["total"] == 1
    assert selected.json()["records"][0]["work_id"] == f["work"]
    assert selected.json()["records"][0]["selected"] is True

    second_page = await client.get(
        f"/api/lists/{f['list']}/acquisition/selected?offset=25&limit=25"
    )
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["total"] == 1
    assert second_page.json()["records"] == []

    async with database() as db:
        monitored = await db.scalar(
            select(ListAcquisitionBook).where(
                ListAcquisitionBook.policy_id == UUID(saved["id"]),
                ListAcquisitionBook.work_id == UUID(f["work"]),
            )
        )
        assert monitored is not None


async def test_select_all_missing_requires_revision_and_cannot_mix_manual_ids(
    client, policy_fixture
):
    f = policy_fixture
    await add(client, f)

    missing_revision = await client.post(
        f"/api/lists/{f['list']}/acquisition/preview",
        json={**f["config"], "select_all_missing": True},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert missing_revision.status_code == 422

    mixed = await client.post(
        f"/api/lists/{f['list']}/acquisition/preview",
        json={
            **f["config"],
            "select_all_missing": True,
            "include_work_ids": [f["work"]],
            "expected_content_revision": "0" * 64,
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert mixed.status_code == 422


async def test_selected_backlog_is_explicit_and_membership_change_invalidates_preview(
    client, database, policy_fixture
):
    f = policy_fixture
    await add(client, f)
    plan = await preview(client, f, include_work_ids=[f["work"]])
    assert plan["selected"] == 1
    await client.delete(f"/api/lists/{f['list']}/entries/{f['work']}")
    response = await client.post(
        f"/api/lists/{f['list']}/acquisition/previews/{plan['id']}/activate"
    )
    assert response.status_code == 409
    async with database() as db:
        assert not await db.scalar(select(ListAcquisitionPolicy.id))


async def test_pause_resume_previews_accumulated_additions_without_implicit_backfill(
    client, database, policy_fixture
):
    f = policy_fixture
    saved = await activate(client, f, await preview(client, f))
    paused = await client.post(
        f"/api/lists/{f['list']}/acquisition/pause", json={"expected_revision": saved["revision"]}
    )
    assert paused.status_code == 200
    await add(client, f)
    await list_automation.schedule()
    assert not f["calls"]
    resumed = await activate(
        client, f, await preview(client, f, expected_revision=paused.json()["revision"])
    )
    await tick(database, resumed)
    assert not f["calls"]
    explicit = await activate(
        client,
        f,
        await preview(
            client, f, expected_revision=resumed["revision"], include_work_ids=[f["work"]]
        ),
    )
    await tick(database, explicit)
    await tick(database, explicit, force_books=True)
    assert f["source"]["qbit"].calls.count("submit") == 1


@pytest.mark.parametrize("change", ["pause", "member", "permission", "list"])
async def test_authority_loss_after_search_cannot_submit(
    client, database, admin, policy_fixture, change, monkeypatch
):
    # Tests advance ticks explicitly. A real minute boundary must not let the
    # worker schedule a second policy pass before the test revokes authority.
    monkeypatch.setattr(list_automation, "next_tick", lambda now: now + timedelta(hours=1))
    f = policy_fixture
    await add(client, f)
    saved = await activate(client, f, await preview(client, f, include_work_ids=[f["work"]]))
    await tick(database, saved)
    await tick(database, saved, worker=False, force_books=True)
    if change == "pause":
        response = await client.post(
            f"/api/lists/{f['list']}/acquisition/pause",
            json={"expected_revision": saved["revision"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["active"] is False
    elif change == "member":
        await client.delete(f"/api/lists/{f['list']}/entries/{f['work']}")
    elif change == "list":
        await client.delete(f"/api/lists/{f['list']}")
    else:
        async with database() as db, db.begin():
            user = await db.get(User, UUID(admin["id"]))
            user.role, user.can_automate = "member", False
    await get_queue().run_worker_async(wait=False, concurrency=1)
    assert f["source"]["qbit"].calls.count("submit") == 0
    async with database() as db:
        assert not await db.scalar(select(DownloadAttempt.id))


async def test_policy_reason_withdrawal_preserves_manual_reason(client, database, policy_fixture):
    f = policy_fixture
    await add(client, f)
    saved = await activate(client, f, await preview(client, f, include_work_ids=[f["work"]]))
    await tick(database, saved, worker=False)
    await client.delete(f"/api/lists/{f['list']}/entries/{f['work']}")
    async with database() as db:
        reasons = list(await db.scalars(select(AcquisitionReason)))
        assert any(r.kind == "manual" and r.active for r in reasons)
        assert any(r.reference.startswith("policy:") and not r.active for r in reasons)


async def test_concurrent_schedulers_coalesce_one_policy_tick(client, database, policy_fixture):
    f = policy_fixture
    await activate(client, f, await preview(client, f))
    await asyncio.gather(list_automation.schedule(), list_automation.schedule())
    async with database() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.kind == list_automation.KIND)
            )
            == 1
        )
