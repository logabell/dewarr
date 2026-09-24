# ruff: noqa: F811
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.hardcover_lists import ListPage
from app.db.models import (
    AcquisitionReason,
    CatalogAccount,
    DownloadAttempt,
    ListCatalogBinding,
    ListEntry,
    ListObservation,
    ListSubscription,
    MonitoredRelease,
    User,
)
from app.domain import hardcover_subscriptions, release_monitor
from app.jobs.retry import ShelfRetry
from app.security import encrypt_secrets
from tests.integration.test_acquisition import catalog  # noqa: F401
from tests.integration.test_acquisition_selections import selection_route  # noqa: F401
from tests.integration.test_automatic_dispatch import authorized  # noqa: F401
from tests.integration.test_automatic_selection import source  # noqa: F401
from tests.integration.test_list_policies import (  # noqa: F401
    activate,
    policy_fixture,
    preview,
    tick,
)

pytestmark = pytest.mark.integration


def record(key, title="A book", **changes):
    return {
        "source_kind": "author",
        "external_id": str(key),
        "title": title,
        "authors": ["Writer"],
        "isbn": None,
        "isbn13": None,
        "filter_reason": None,
        "release_date": None,
        **changes,
    }


@pytest.fixture
async def follow_fixture(client, database, admin, monkeypatch):
    async with database() as db, db.begin():
        db.add(
            CatalogAccount(
                user_id=UUID(admin["id"]),
                enabled=True,
                generation=1,
                encrypted_token=encrypt_secrets({"token": "fixture-only"}),
            )
        )

    class Catalog:
        records = [record(10, "Earlier book")]
        fail = False

        async def page(self, owner, generation, token, external, cursor, *, follow):
            if self.fail:
                raise AdapterError(FailureKind.PARSER, "Incomplete catalog")
            return ListPage(
                {"name": "Writer", "external_id": external, "count": len(self.records)},
                deepcopy([r for r in self.records if int(r["external_id"]) > cursor]),
                max([cursor, *(int(r["external_id"]) for r in self.records)]),
            )

    remote = Catalog()
    monkeypatch.setattr(hardcover_subscriptions, "fetch_page", remote.page)
    created = await client.post(
        "/api/following", json={"source_kind": "author", "external_id": 9, "name": "Writer"}
    )
    assert created.status_code == 201, created.text
    return remote, created.json()


async def observe(client, database, follow, *, initial=False):
    if not initial:
        response = await client.post(
            f"/api/lists/{follow['list_id']}/subscription/sync",
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202, response.text
    async with database() as db:
        row = await db.get(ListSubscription, UUID(follow["subscription"]["id"]))
        operation = row.operation_id
    for _ in range(10):
        try:
            await hardcover_subscriptions.run(operation)
            return
        except ShelfRetry:
            pass
    pytest.fail("Follow catalog failed to finish in ten pages")


async def bind_work(database, admin, key, work_id):
    async with database() as db, db.begin():
        db.add(
            ListCatalogBinding(
                owner_id=UUID(admin["id"]),
                identity_key=f"hardcover:{key}",
                work_id=UUID(work_id),
                assertion={"title": "A book", "authors": ["Writer"], "isbn": None, "isbn13": None},
            )
        )


async def test_successful_baseline_required_and_failed_refresh_preserves_membership(
    client, database, follow_fixture, policy_fixture
):
    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    response = await client.post(
        f"/api/lists/{f['list']}/acquisition/preview",
        json=f["config"],
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 409
    remote.fail = True
    await observe(client, database, follow, initial=True)
    async with database() as db:
        assert (
            await db.get(ListSubscription, UUID(follow["subscription"]["id"]))
        ).baseline_at is None
        assert await db.scalar(select(func.count()).select_from(ListEntry)) == 0
    remote.fail = False
    await observe(client, database, follow)
    plan = await preview(client, f)
    assert plan["selected"] == 0 and plan["total"] == 1
    await activate(client, f, plan)
    remote.records = []
    remote.fail = True
    await observe(client, database, follow)
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(ListEntry)) == 1


async def test_future_only_requests_new_book_once_and_preserves_other_reasons(
    client, database, admin, follow_fixture, policy_fixture
):
    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    await observe(client, database, follow, initial=True)
    saved = await activate(client, f, await preview(client, f))
    await tick(database, saved)
    assert f["calls"] == []
    await bind_work(database, admin, 20, f["work"])
    remote.records.append(record(20))
    await observe(client, database, follow)
    await observe(client, database, follow)
    await tick(database, saved)
    await tick(database, saved, force_books=True)
    await tick(database, saved, force_books=True)
    assert f["source"]["qbit"].calls.count("submit") == 1
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
        assert await db.scalar(select(func.count()).select_from(ListObservation)) == 2
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AcquisitionReason)
                .where(AcquisitionReason.list_id == UUID(f["list"]))
            )
            == 1
        )
    # An ordinary list adds a second reason behind the same intent.
    other = (await client.post("/api/lists", json={"name": "Other reason"})).json()["id"]
    assert (
        await client.post(f"/api/lists/{other}/entries", json={"work_id": f["work"]})
    ).status_code == 204
    response = await client.post(
        "/api/requests",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "work_id": f["work"],
            "specification": saved["configuration"]["specification"],
            "reason": {"list_id": other},
        },
    )
    assert response.status_code == 202, response.text
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(DownloadAttempt)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AcquisitionReason)
                .where(AcquisitionReason.list_id.in_([UUID(f["list"]), UUID(other)]))
            )
            == 2
        )


async def test_back_catalog_preview_exclusions_and_unfollow(
    client, database, admin, follow_fixture, policy_fixture
):
    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    await bind_work(database, admin, 20, f["work"])
    remote.records.extend([record(20), record(30, "Box set", filter_reason="Box set")])
    await observe(client, database, follow, initial=True)
    plan = await preview(client, f, include_work_ids=[f["work"]])
    assert plan["total"] == 2 and plan["selected"] == 1 and plan["counts"]["excluded"] == 1
    saved = await activate(client, f, plan)
    await tick(database, saved)
    observations = (await client.get(f"/api/lists/{f['list']}/subscription/observations")).json()[
        "items"
    ]
    book = next(o for o in observations if o["external_id"] == "20")
    assert (
        await client.patch(
            f"/api/lists/{f['list']}/subscription/observations/{book['id']}",
            json={"excluded": True},
        )
    ).status_code == 204
    await observe(client, database, follow)
    async with database() as db:
        assert (await db.get(ListObservation, UUID(book["id"]))).excluded
        assert not await db.scalar(
            select(ListEntry).where(
                ListEntry.list_id == UUID(f["list"]), ListEntry.work_id == UUID(f["work"])
            )
        )
    assert (await client.delete(f"/api/following/{f['list']}")).status_code == 204
    assert (await client.get("/api/following")).json() == []
    async with database() as db:
        assert (await db.get(ListObservation, UUID(book["id"]))).excluded
        assert not await db.scalar(
            select(AcquisitionReason).where(
                AcquisitionReason.active.is_(True), AcquisitionReason.list_id == UUID(f["list"])
            )
        )


async def test_series_waits_for_release_monitor_without_early_source_search(
    client, database, admin, follow_fixture, policy_fixture
):
    remote, follow = follow_fixture
    async with database() as db, db.begin():
        row = await db.get(ListSubscription, UUID(follow["subscription"]["id"]))
        from app.security import decrypt_secrets

        config = decrypt_secrets(row.encrypted_config)
        row.source_kind = "series"
        row.encrypted_config = encrypt_secrets({**config, "source_kind": "series"})
    f = {**policy_fixture, "list": follow["list_id"]}
    await observe(client, database, follow, initial=True)
    saved = await activate(client, f, await preview(client, f))
    await bind_work(database, admin, 20, f["work"])
    remote.records.append(
        record(20, release_date=(datetime.now(UTC).date() + timedelta(days=7)).isoformat())
    )
    await observe(client, database, follow)
    await tick(database, saved)
    assert f["calls"] == []
    async with database() as db, db.begin():
        monitor = await db.scalar(select(MonitoredRelease))
        assert monitor.state == "waiting"
    remote.records[-1]["release_date"] = datetime.now(UTC).date().isoformat()
    await observe(client, database, follow)
    await release_monitor.schedule()
    await tick(database, saved)
    assert f["calls"]


async def test_automatic_follow_respects_request_approval(
    client, database, admin, follow_fixture, policy_fixture
):
    from app.domain.permissions import AUTOMATE, REQUESTER

    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    await observe(client, database, follow, initial=True)
    async with database() as db, db.begin():
        from app.db.models import AutomaticImportPolicy

        owner = await db.get(User, UUID(admin["id"]))
        approver = User(
            username="route-approver",
            display_name="Route approver",
            role="admin",
            password_hash=owner.password_hash,
        )
        db.add(approver)
        await db.flush()
        route_policy = await db.scalar(select(AutomaticImportPolicy))
        route_policy.approved_by = approver.id
    saved = await activate(client, f, await preview(client, f))
    await bind_work(database, admin, 20, f["work"])
    remote.records.append(record(20))
    await observe(client, database, follow)
    async with database() as db, db.begin():
        user = await db.get(User, UUID(admin["id"]))
        user.role, user.permissions, user.can_automate = "member", REQUESTER | AUTOMATE, True
        from app.db.models import LibraryGrant

        db.add(
            LibraryGrant(
                user_id=user.id,
                library_id=UUID(saved["configuration"]["specification"]["audio_library_id"]),
            )
        )
        for reason in await db.scalars(select(AcquisitionReason)):
            reason.active = False
    await tick(database, saved, worker=False)
    async with database() as db:
        reason = await db.scalar(
            select(AcquisitionReason).where(AcquisitionReason.list_id == UUID(f["list"]))
        )
        from app.db.models import ListAcquisitionBook, ListAcquisitionPolicy

        policy = await db.get(ListAcquisitionPolicy, UUID(saved["id"]))
        messages = [
            (b.state, b.message)
            for b in await db.scalars(
                select(ListAcquisitionBook).where(ListAcquisitionBook.policy_id == policy.id)
            )
        ]
        assert reason and reason.approval_status == "pending", (policy.message, messages)
        assert not await db.scalar(select(DownloadAttempt))
    assert f["calls"] == []


async def test_duplicate_follow_and_filter_edit_pause_policy(
    client, database, follow_fixture, policy_fixture
):
    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    again = await client.post(
        "/api/following", json={"source_kind": "author", "external_id": 9, "name": "Writer"}
    )
    assert again.json()["list_id"] == follow["list_id"]
    await observe(client, database, follow, initial=True)
    await activate(client, f, await preview(client, f))
    changed = await client.patch(
        f"/api/following/{f['list']}",
        json={"expected_generation": 1, "enabled": True, "filters": {"compilations": True}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["active"] is False
    assert changed.json()["subscription"]["completeness"] == "not-observed"
    stale = await client.patch(
        f"/api/following/{f['list']}",
        json={"expected_generation": 1, "enabled": False, "filters": {}},
    )
    assert stale.status_code == 409


async def test_follow_source_is_read_only_and_not_a_hardcover_list(
    client, database, admin, follow_fixture
):
    from fastapi import HTTPException

    from app.domain.community_lists import followed
    from app.domain.list_writeback import binding

    _, follow = follow_fixture
    await observe(client, database, follow, initial=True)
    url = f"/api/lists/{follow['list_id']}/subscription"
    assert (
        await client.put(url, json={"enabled": True, "expected_generation": 1})
    ).status_code == 422
    assert (await client.delete(url)).status_code == 422
    assert (await client.get("/api/reading-accounts/subscriptions")).json() == []
    async with database() as db:
        assert await followed(db, UUID(admin["id"])) == {}
        row = await db.get(ListSubscription, UUID(follow["subscription"]["id"]))
        assert row.source_kind == "author"
        with pytest.raises(HTTPException, match="read-only"):
            binding(await db.get(CatalogAccount, UUID(admin["id"])), row)


async def test_removed_and_restored_baseline_book_does_not_become_new(
    client, database, follow_fixture, policy_fixture
):
    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    await observe(client, database, follow, initial=True)
    saved = await activate(client, f, await preview(client, f))
    remote.records = []
    await observe(client, database, follow)
    await tick(database, saved, worker=False)
    remote.records = [record(10, "Earlier book")]
    await observe(client, database, follow)
    await tick(database, saved, worker=False)
    assert not f["calls"]
    async with database() as db:
        assert not await db.scalar(
            select(AcquisitionReason).where(AcquisitionReason.list_id == UUID(f["list"]))
        )


async def test_discovery_event_contract_omits_baseline_failed_filtered_and_repeated_reads(
    client, database, admin, follow_fixture, monkeypatch
):
    import sys
    from types import ModuleType

    calls = []
    module = ModuleType("app.notifications.events")

    async def record_event(db, **payload):
        calls.append(payload)

    module.record_event = record_event
    monkeypatch.setitem(sys.modules, "app.notifications", ModuleType("app.notifications"))
    monkeypatch.setitem(sys.modules, "app.notifications.events", module)
    remote, follow = follow_fixture
    await observe(client, database, follow, initial=True)
    assert calls == []
    remote.records += [
        record(20, "New novel"),
        record(30, "Collection", filter_reason="Compilation"),
    ]
    remote.fail = True
    await observe(client, database, follow)
    assert calls == []
    remote.fail = False
    await observe(client, database, follow)
    await observe(client, database, follow)
    assert len(calls) == 1
    payload = calls[0]
    assert payload["event_type"] == "discovery.author"
    assert payload["owner_id"] == UUID(admin["id"])
    assert payload["message"] == "New novel"
    assert payload["key"] == f"follow:{follow['subscription']['id']}:work:{payload['subject_id']}"
    assert payload["path"] == f"/books/{payload['subject_id']}"
    assert "fixture-only" not in str(payload)


async def test_revoked_request_permission_prevents_follow_dispatch(
    client, database, admin, follow_fixture, policy_fixture
):
    from app.db.models import ListAcquisitionPolicy
    from app.domain.permissions import AUTOMATE

    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    await observe(client, database, follow, initial=True)
    saved = await activate(client, f, await preview(client, f))
    remote.records.append(record(20))
    await observe(client, database, follow)
    async with database() as db, db.begin():
        user = await db.get(User, UUID(admin["id"]))
        user.role, user.permissions = "member", AUTOMATE
    await tick(database, saved, worker=False)
    async with database() as db:
        policy = await db.get(ListAcquisitionPolicy, UUID(saved["id"]))
        assert "cannot request books" in policy.message
        assert not await db.scalar(
            select(AcquisitionReason).where(AcquisitionReason.list_id == UUID(f["list"]))
        )


async def test_follow_is_owner_scoped(client, database, admin, follow_fixture):
    from app.db.models import BookList

    _, follow = follow_fixture
    async with database() as db, db.begin():
        owner = await db.get(User, UUID(admin["id"]))
        other = User(
            username="other", display_name="Other", role="member", password_hash=owner.password_hash
        )
        db.add(other)
        await db.flush()
        item = await db.get(BookList, UUID(follow["list_id"]))
        item.owner_id = other.id
    assert (await client.get("/api/following")).json() == []
    assert (
        await client.patch(
            f"/api/following/{follow['list_id']}",
            json={"expected_generation": 1, "enabled": False, "filters": {}},
        )
    ).status_code == 404
    assert (await client.delete(f"/api/following/{follow['list_id']}")).status_code == 404


async def test_author_follow_does_not_replace_a_separate_book_release_follow(
    client, database, admin, follow_fixture, policy_fixture
):
    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    await observe(client, database, follow, initial=True)
    saved = await activate(client, f, await preview(client, f))
    await bind_work(database, admin, 20, f["work"])
    day = (datetime.now(UTC).date() + timedelta(days=7)).isoformat()
    response = await client.post(
        "/api/releases/follow",
        json={"work_id": f["work"], "mode": "audio", "release_date": day, "basis": "work"},
    )
    assert response.status_code == 201, response.text
    async with database() as db:
        original = (await db.scalar(select(MonitoredRelease))).operation_id
    remote.records.append(record(20, release_date=day))
    await observe(client, database, follow)
    await tick(database, saved)
    assert (await client.delete(f"/api/following/{f['list']}")).status_code == 204
    async with database() as db:
        monitor = await db.scalar(select(MonitoredRelease))
        assert monitor.operation_id == original and monitor.state == "waiting"


async def test_refollow_requires_new_baseline_before_future_only_activation(
    client, database, admin, follow_fixture, policy_fixture
):
    remote, follow = follow_fixture
    f = {**policy_fixture, "list": follow["list_id"]}
    await observe(client, database, follow, initial=True)
    await activate(client, f, await preview(client, f))
    assert (await client.delete(f"/api/following/{f['list']}")).status_code == 204
    await bind_work(database, admin, 20, f["work"])
    remote.records.append(record(20, "Published while unfollowed"))
    response = await client.post(
        "/api/following", json={"source_kind": "author", "external_id": 9, "name": "Writer"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["subscription"]["completeness"] == "not-observed"
    blocked = await client.post(
        f"/api/lists/{f['list']}/acquisition/preview",
        json={**f["config"], "expected_revision": 2},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert blocked.status_code == 409
    await observe(client, database, follow, initial=True)
    plan = await preview(client, f, expected_revision=2)
    assert plan["total"] == 2 and plan["selected"] == 0
    saved = await activate(client, f, plan)
    await tick(database, saved)
    assert not f["calls"]
