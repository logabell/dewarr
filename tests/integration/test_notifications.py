# ruff: noqa: F811
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    Operation,
    User,
    Work,
)
from app.notifications import channels
from app.notifications.delivery import deliver_one, route_events, tick
from app.notifications.events import record_event
from tests.integration.test_request_approvals import session_for

pytestmark = pytest.mark.integration


async def channel(client, events, **extra):
    response = await client.post(
        "/api/notifications/channels",
        json={
            "name": "Controlled fixture",
            "kind": "webhook",
            "events": events,
            "secrets": {"url": "https://fixture.invalid/hook", "token": "private-fixture-token"},
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    assert "private-fixture-token" not in response.text
    assert "fixture.invalid" not in response.text
    return response.json()


async def emit(db, owner, key, kind="discovery.author"):
    await record_event(
        db,
        key=key,
        event_type=kind,
        owner_id=owner,
        subject_id=uuid4(),
        title="A new book",
        message="Safe fixture detail",
        path="/requests",
    )


@pytest.fixture
async def sent(monkeypatch):
    calls = []

    async def send(kind, config, payload, **kwargs):
        calls.append(payload)

    monkeypatch.setattr(channels, "send", send)
    return calls


async def test_transaction_rollback_and_repeated_worker_are_idempotent(
    client, admin, database, sent
):
    await channel(client, ["request.pending", "request.approved"], installation=True)
    async with database() as db, db.begin():
        work = Work(title="A request")
        db.add(work)
        await db.flush()
        intent = AcquisitionIntent(
            owner_id=UUID(admin["id"]), work_id=work.id, fingerprint="fixture", specification={}
        )
        db.add(intent)
        await db.flush()
        reason = AcquisitionReason(
            intent_id=intent.id, kind="manual", reference="fixture", approval_status="pending"
        )
        db.add(reason)
    async with database() as db:
        await emit(db, UUID(admin["id"]), "rolled-back", "request.approved")
        await db.rollback()
    await tick()
    async with database() as db, db.begin():
        row = await db.get(AcquisitionReason, reason.id)
        row.approval_status = "approved"
    await tick()
    await tick()
    async with database() as db, db.begin():
        await db.execute(
            update(AcquisitionReason)
            .where(AcquisitionReason.id == reason.id)
            .values(approval_status="approved")
        )
    await tick()
    assert [payload["events"][0]["type"] for payload in sent] == [
        "request.pending",
        "request.approved",
    ]
    async with database() as db:
        assert await db.scalar(select(func.count()).select_from(NotificationEvent)) == 2
        saved = await db.scalar(select(NotificationChannel))
        assert "private-fixture-token" not in saved.encrypted_secrets


async def test_private_routing_policy_and_channel_permissions(client, admin, database, sent):
    created = await client.post(
        "/api/auth/users",
        json={
            "username": "notify-member",
            "display_name": "Reader",
            "password": "long fixture password",
            "role": "member",
        },
    )
    owner = UUID(created.json()["id"])
    async with aclosing(await session_for("notify-member", "long fixture password")) as member:
        personal = await channel(member, ["discovery.author"], digest_minutes=0)
        forbidden = await member.post(
            "/api/notifications/channels",
            json={
                "name": "Forbidden",
                "kind": "webhook",
                "installation": True,
                "events": [],
                "secrets": {"url": "https://fixture.invalid"},
            },
        )
        assert forbidden.status_code == 403
        assert (
            await member.put("/api/notifications/policy", json={"member_events": []})
        ).status_code == 403
        admin_channel = await channel(client, ["discovery.author"], digest_minutes=0)
        assert (
            await member.delete("/api/notifications/channels/" + admin_channel["id"])
        ).status_code == 404
        async with database() as db, db.begin():
            await emit(db, owner, "member-event")
            await emit(db, UUID(admin["id"]), "admin-event")
        await tick()
        assert len(sent) == 2
        assert all(len(item["events"]) == 1 for item in sent)
        assert all("owner" not in str(item) and "token" not in str(item) for item in sent)
        async with database() as db, db.begin():
            await emit(db, owner, "revoked-before-delivery")
            await route_events(db)
        assert (
            await client.put("/api/notifications/policy", json={"member_events": []})
        ).status_code == 200
        await tick()
        assert len(sent) == 2
        history = (
            await member.get("/api/notifications/channels/" + personal["id"] + "/deliveries")
        ).json()
        assert history[0]["state"] == "cancelled"


async def test_apprise_requires_admin_at_configuration_and_delivery(
    client, admin, database, monkeypatch
):
    import apprise

    async def unexpected_notify(*args, **kwargs):
        pytest.fail("Demoted owner's Apprise channel reached the plugin transport")

    monkeypatch.setattr(apprise.Apprise, "async_notify", unexpected_notify)
    created = await client.post(
        "/api/auth/users",
        json={
            "username": "notify-demoted",
            "display_name": "Former administrator",
            "password": "long fixture password",
            "role": "admin",
        },
    )
    assert created.status_code == 201, created.text
    owner = UUID(created.json()["id"])
    async with aclosing(await session_for("notify-demoted", "long fixture password")) as member:
        saved = await channel(
            member,
            [],
            kind="apprise",
            secrets={"urls": ["json://notifications.example/hook"]},
        )
        route = "/api/notifications/channels/" + saved["id"]
        assert (await member.post(route + "/test")).status_code == 202
        async with database() as db, db.begin():
            (await db.get(User, owner)).role = "member"
        assert await deliver_one()
        history = (await member.get(route + "/deliveries")).json()
        assert history[0]["state"] == "failed"
        assert "requires an administrator" in history[0]["message"]
        assert (await member.post(route + "/test")).status_code == 403
        body = {"name": saved["name"], "kind": "apprise", "events": [], "enabled": True}
        assert (await member.put(route, json=body)).status_code == 403
        assert (
            await member.put(
                route,
                json={
                    **body,
                    "enabled": False,
                    "secrets": {"urls": ["json://notifications.example/replacement"]},
                },
            )
        ).status_code == 403
        assert (
            await member.post(
                "/api/notifications/channels",
                json={**body, "secrets": {"urls": ["json://notifications.example/hook"]}},
            )
        ).status_code == 403
        # The old channel remains manageable without permitting another delivery.
        assert (await member.put(route, json={**body, "enabled": False})).status_code == 200
        switched = await member.put(
            route,
            json={**body, "kind": "webhook", "secrets": {"url": "https://example.com/hook"}},
        )
        assert switched.status_code == 200, switched.text
        assert (
            await member.put(
                route,
                json={
                    **body,
                    "enabled": False,
                    "secrets": {"urls": ["json://notifications.example/hook"]},
                },
            )
        ).status_code == 403
        assert (await member.delete(route)).status_code == 204


async def test_discovery_digest_batches_two_hundred_events(client, admin, database, sent):
    await channel(client, ["discovery.author"], digest_minutes=15)
    async with database() as db, db.begin():
        for index in range(200):
            await emit(db, UUID(admin["id"]), f"new-book:{index}")
        await route_events(db)
    assert not await deliver_one()
    async with database() as db, db.begin():
        await db.execute(
            update(NotificationDelivery).values(due_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await tick()
    assert len(sent) == 1 and len(sent[0]["events"]) == 200
    await tick()
    assert len(sent) == 1


async def test_failed_and_interrupted_deliveries_do_not_retry_or_affect_producer(
    client, admin, database, monkeypatch
):
    saved = await channel(client, ["operation.failed"])
    async with database() as db, db.begin():
        operation = Operation(
            owner_id=UUID(admin["id"]),
            kind="fixture",
            idempotency_key="failure",
            status="failed",
            message="Safe failure",
        )
        db.add(operation)

    async def fail(*args, **kwargs):
        raise channels.DeliveryError("Destination returned HTTP 503")

    monkeypatch.setattr(channels, "send", fail)
    await tick()
    history = (
        await client.get("/api/notifications/channels/" + saved["id"] + "/deliveries")
    ).json()
    assert history[0]["state"] == "failed"
    async with database() as db, db.begin():
        row = await db.scalar(select(NotificationDelivery))
        row.state = "sending"
        row.attempted_at = datetime.now(UTC) - timedelta(minutes=3)
        assert (await db.get(Operation, operation.id)).status == "failed"
    await tick()
    history = (
        await client.get("/api/notifications/channels/" + saved["id"] + "/deliveries")
    ).json()
    assert history[0]["state"] == "uncertain"


async def test_test_button_queues_and_never_returns_secrets(client, admin, database, sent):
    saved = await channel(client, [])
    response = await client.post("/api/notifications/channels/" + saved["id"] + "/test")
    assert response.status_code == 202 and response.json()["last_status"] == "pending"
    assert not sent
    assert (
        await client.post("/api/notifications/channels/" + saved["id"] + "/test")
    ).status_code == 429
    await tick()
    assert len(sent) == 1 and sent[0]["events"][0]["type"] == "test"
    assert (await client.get("/api/notifications")).json()["channels"][0]["last_status"] == "sent"


async def test_queue_registration(database):
    from app.jobs.queue import get_queue

    assert "notifications.dispatch" in get_queue().tasks


async def test_transient_flush_and_duplicate_request_reasons_do_not_notify(
    client, admin, database, sent
):
    await channel(client, ["operation.failed", "request.pending"], installation=True)
    async with database() as db, db.begin():
        op = Operation(
            owner_id=UUID(admin["id"]),
            kind="fixture",
            idempotency_key="transient",
            status="failed",
            message="Temporary",
        )
        db.add(op)
        await db.flush()
        op.status = "completed"
        work = Work(title="Multiple reasons")
        db.add(work)
        await db.flush()
        intent = AcquisitionIntent(
            owner_id=UUID(admin["id"]), work_id=work.id, fingerprint="multi", specification={}
        )
        db.add(intent)
        await db.flush()
        db.add(
            AcquisitionReason(
                intent_id=intent.id, kind="manual", reference="first", approval_status="pending"
            )
        )
    await tick()
    async with database() as db, db.begin():
        db.add(
            AcquisitionReason(
                intent_id=intent.id, kind="manual", reference="second", approval_status="pending"
            )
        )
    await tick()
    assert len(sent) == 1 and sent[0]["events"][0]["type"] == "request.pending"


async def test_worker_cancellation_after_attempt_commit_never_resends(
    client, admin, database, monkeypatch
):
    import asyncio

    saved = await channel(client, ["operation.failed"])
    async with database() as db, db.begin():
        await emit(db, UUID(admin["id"]), "crash", "operation.failed")
    calls = []

    async def stopped(*args, **kwargs):
        calls.append(True)
        raise asyncio.CancelledError()

    monkeypatch.setattr(channels, "send", stopped)
    with pytest.raises(asyncio.CancelledError):
        await tick()
    async with database() as db, db.begin():
        row = await db.scalar(select(NotificationDelivery))
        assert row.state == "sending"
        row.attempted_at = datetime.now(UTC) - timedelta(minutes=3)
    await tick()
    assert len(calls) == 1
    assert (await client.get("/api/notifications/channels/" + saved["id"] + "/deliveries")).json()[
        0
    ]["state"] == "uncertain"


async def test_restore_boundary_cancels_queued_and_unrouted_history(client, admin, database, sent):
    from app.db.models import RestoreCheckpoint

    await channel(client, ["discovery.author"], digest_minutes=0)
    async with database() as db, db.begin():
        await emit(db, UUID(admin["id"]), "queued-before-restore")
        await route_events(db)
    async with database() as db, db.begin():
        await emit(db, UUID(admin["id"]), "unrouted-before-restore")
    async with database() as db, db.begin():
        db.add(
            RestoreCheckpoint(
                operator_id=UUID(admin["id"]), backup_id=uuid4(), active=False, snapshot={}
            )
        )
    await tick()
    assert not sent
    async with database() as db:
        assert (await db.scalar(select(NotificationDelivery))).state == "cancelled"
        assert not list(
            await db.scalars(select(NotificationEvent).where(NotificationEvent.routed_at.is_(None)))
        )


async def test_list_baseline_is_silent_and_new_match_notifies(client, admin, database, sent):
    from app.db.models import BookList, ListEntry, ListSubscription

    await channel(client, ["discovery.list"], digest_minutes=0)
    async with database() as db, db.begin():
        book_list = BookList(owner_id=UUID(admin["id"]), name="Fixture list")
        old = Work(title="Baseline book")
        db.add_all([book_list, old])
        await db.flush()
        subscription = ListSubscription(list_id=book_list.id, encrypted_config="unused")
        db.add(subscription)
        db.add(ListEntry(list_id=book_list.id, work_id=old.id, locally_added=False))
        await db.flush()
        subscription.baseline_at = datetime.now(UTC)
    await tick()
    assert not sent
    async with database() as db, db.begin():
        new = Work(title="New discovery")
        db.add(new)
        await db.flush()
        db.add(ListEntry(list_id=book_list.id, work_id=new.id, locally_added=False))
    await tick()
    assert len(sent) == 1
    assert sent[0]["events"][0]["type"] == "discovery.list"
    assert "New discovery" in sent[0]["events"][0]["message"]


async def test_expected_release_wait_does_not_notify_as_review(client, admin, database, sent):
    await channel(client, ["operation.held"])
    async with database() as db, db.begin():
        db.add_all(
            [
                Operation(
                    owner_id=UUID(admin["id"]),
                    kind="acquisition.quick-add",
                    idempotency_key="wait-quick",
                    status="held",
                    payload={"waiting_for_release": None},
                ),
                Operation(
                    owner_id=UUID(admin["id"]),
                    kind="lists.release-wait",
                    idempotency_key="wait-follow",
                    status="held",
                    payload={"waiting_for_release": "2027-01-01"},
                ),
            ]
        )
    await tick()
    assert not sent
