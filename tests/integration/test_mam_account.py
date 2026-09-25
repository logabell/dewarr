from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import AuditEvent, User
from tests.integration.test_mam_sources import configure
from tests.integration.test_mam_sources import source_http as mam_http_fixture

source_http = mam_http_fixture

pytestmark = pytest.mark.integration


async def test_account_is_private_allowlisted_and_generation_checked(client, admin, source_http):
    await configure(client)
    source_http["body"] = {
        "uid": 99,
        "username": "Mouse",
        "classname": "Power User",
        "ratio": "2.75",
        "seedbonus": 75000,
        "uploaded": "4 TiB",
        "downloaded": "1.5 TiB",
        "secret": "never expose",
    }
    response = await client.get("/api/sources/mam/account", params={"expected_generation": 1})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["username"] == "Mouse" and response.json()["seedbonus"] == 75000
    assert "secret" not in response.text
    assert (
        await client.get("/api/sources/mam/account", params={"expected_generation": 0})
    ).status_code == 409
    assert len(source_http["calls"]) == 1


async def test_purchase_duplicate_and_stale_requests_never_spend_again(
    client, admin, database, source_http
):
    await configure(client)
    # The fixture uses one body for preflight and purchase; include an explicit acknowledgement.
    source_http["body"] = {"uid": 99, "username": "Mouse", "seedbonus": 75000, "success": True}
    body = {"request_id": str(uuid4()), "expected_generation": 1, "kind": "upload", "amount": "max"}
    response = await client.post("/api/sources/mam/purchases", json=body)
    assert response.status_code == 200 and response.json()["status"] == "completed", response.text
    assert len(source_http["calls"]) == 2
    assert source_http["calls"][-1].url.params["amount"] == "Max Affordable "
    assert (await client.post("/api/sources/mam/purchases", json=body)).status_code == 409
    assert (
        await client.post(
            "/api/sources/mam/purchases",
            json={**body, "request_id": str(uuid4()), "expected_generation": 0},
        )
    ).status_code == 409
    assert len(source_http["calls"]) == 2
    async with database() as db:
        event = await db.scalar(
            select(AuditEvent).where(AuditEvent.action == "source.mam.purchase")
        )
        assert event.detail == {"kind": "upload", "amount": "max", "status": "completed"}


async def test_account_and_purchases_require_admin(client, admin, database, source_http):
    await configure(client)
    async with database() as db, db.begin():
        user = await db.scalar(select(User).where(User.username == "admin"))
        user.role = "member"
    assert (
        await client.get("/api/sources/mam/account", params={"expected_generation": 1})
    ).status_code == 403
    body = {"request_id": str(uuid4()), "expected_generation": 1, "kind": "VIP"}
    assert (await client.post("/api/sources/mam/purchases", json=body)).status_code == 403
    assert not source_http["calls"]


async def test_purchase_failure_is_not_replayed_on_direct_fallback(
    client, admin, source_http, monkeypatch
):
    from app.adapters.contracts import AdapterError, FailureKind
    from app.adapters.mam import MAMClient

    attempts = []

    async def fail_purchase(self, body):
        attempts.append(body.request_id)
        error = AdapterError(FailureKind.ROUTE, "Proxy could not complete the request")
        error.proxy_retryable = True
        raise error

    monkeypatch.setattr(MAMClient, "purchase", fail_purchase)
    await configure(client, proxy_url="http://proxy.test", proxy_fallback_direct=True)
    body = {"request_id": str(uuid4()), "expected_generation": 1, "kind": "VIP"}
    assert (await client.post("/api/sources/mam/purchases", json=body)).status_code >= 400
    assert len(attempts) == 1
    assert (await client.post("/api/sources/mam/purchases", json=body)).status_code == 409
    assert len(attempts) == 1


async def test_seedbox_queued_command_cannot_bypass_hourly_limit(
    client, admin, database, source_http
):
    from datetime import UTC, datetime
    from uuid import UUID

    from fastapi import HTTPException

    from app.adapters.mam import HelperCommand
    from app.db.models import SourceConnection
    from app.domain.source_network import source_call

    await configure(client, automation={"seedbox_ip": True})
    stamp = datetime.now(UTC).isoformat()
    async with database() as db, db.begin():
        row = await db.get(SourceConnection, "mam")
        row.automation_state = {"seedbox_at": stamp, "upload_at": stamp}
    with pytest.raises(HTTPException) as error:
        await source_call(UUID(admin["id"]), "maintain", HelperCommand(seedbox=True))
    assert error.value.status_code == 429 and not source_http["calls"]
    response = await client.get("/api/sources/mam/connection")
    assert response.json()["automation_checks"]["upload"] is not None
