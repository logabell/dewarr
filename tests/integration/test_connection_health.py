# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.db.models import Integration, SourceConnection
from app.domain import connection_health
from app.domain.mam_diagnostics import EgressResult
from tests.integration.test_downloaders import create, downloader_http  # noqa: F401
from tests.integration.test_mam_sources import configure, source_http  # noqa: F401


async def age_connections(database):
    from sqlalchemy import update

    async with database() as db, db.begin():
        for model in (Integration, SourceConnection):
            await db.execute(
                update(model).values(last_checked_at=datetime.now(UTC) - timedelta(minutes=6))
            )
        await db.execute(update(Integration).values(next_sync_at=None))
        await db.execute(update(SourceConnection).values(next_request_at=None, blocked_until=None))


async def test_periodic_failure_recovery_and_manual_test_share_health(
    client, admin, database, source_http, downloader_http
):
    await configure(client)
    downloader = await create(client)
    initial = (await client.get("/api/health/connections")).json()
    assert initial["issues"] == 2
    await connection_health.run()
    healthy = (await client.get("/api/health/connections")).json()
    assert healthy["issues"] == 0
    assert all(item["checked_at"] for item in healthy["connections"])
    # No unnecessary HTTP calls while a result is fresh.
    before = len(downloader_http["calls"])
    await connection_health.run()
    assert len(downloader_http["calls"]) == before
    await age_connections(database)
    source_http["status"] = 403
    downloader_http["status"] = 403
    await connection_health.run()
    failed = (await client.get("/api/health/connections")).json()
    assert failed["issues"] == 2
    assert all(item["status"] == "authentication" for item in failed["connections"])
    assert "proxy IP" in failed["connections"][0]["message"]
    assert (await client.get("/api/sources/mam/connection")).json()["status"] == "authentication"
    assert "private-qbit" not in str(failed)
    await age_connections(database)
    source_http["status"] = 200
    await configure(client, expected_generation=1, mam_id=source_http["cookie"])
    downloader_http["status"] = 204
    await connection_health.run()
    assert (await client.get("/api/health/connections")).json()["issues"] == 0
    # Manual tests immediately update the same snapshot, including failures.
    await age_connections(database)
    downloader_http["status"] = 403
    await client.post(f"/api/downloaders/{downloader['id']}/test")
    assert (await client.get("/api/health/connections")).json()["issues"] == 1


async def test_stale_disabled_deleted_and_missing_session(
    client, admin, database, source_http, downloader_http
):
    await configure(client)
    downloader = await create(client)
    await connection_health.run()
    async with database() as db, db.begin():
        mam = await db.get(SourceConnection, "mam")
        mam.last_checked_at = datetime.now(UTC) - timedelta(minutes=11)
        dl = await db.get(Integration, UUID(downloader["id"]))
        dl.enabled = False
    snapshot = (await client.get("/api/health/connections")).json()
    assert snapshot["issues"] == 1 and snapshot["connections"][0]["status"] == "stale"
    assert (await client.get("/api/sources/mam/connection")).json()["status"] == "stale"
    async with database() as db, db.begin():
        mam = await db.get(SourceConnection, "mam")
        mam.deleted_at = datetime.now(UTC)
    assert (await client.get("/api/health/connections")).json()["connections"] == []


async def test_proxy_probe_is_independent_and_generation_fenced(
    client, admin, database, monkeypatch
):
    await configure(client, proxy_url="http://proxy.test:8888", mam_id=None)
    state = {"fail": True}

    async def probe(*args):
        return (
            EgressResult(error="Proxy unavailable")
            if state["fail"]
            else EgressResult(ip="203.0.113.12")
        )

    monkeypatch.setattr(connection_health, "probe_egress", probe)
    await connection_health.run()
    failed = (await client.get("/api/health/connections")).json()
    assert failed["issues"] == 2
    assert failed["connections"][0]["status"] == "authentication"
    state["fail"] = False
    await connection_health.run()
    working_proxy = (await client.get("/api/health/connections")).json()
    assert working_proxy["issues"] == 1
    assert working_proxy["connections"][1]["status"] == "connected"
    assert (await client.get("/api/sources/mam/connection")).json()["proxy_health"][
        "ip"
    ] == "203.0.113.12"
    await configure(
        client, expected_generation=1, proxy_url="http://new-proxy.test:8888", mam_id=None
    )
    await connection_health.record_proxy(1, EgressResult(ip="203.0.113.99"))
    assert (await client.get("/api/sources/mam/connection")).json()["proxy_health"][
        "status"
    ] == "untested"


async def test_busy_mam_does_not_hide_downloader_failure(
    client, admin, database, source_http, downloader_http
):
    from uuid import uuid4

    await configure(client)
    await create(client)
    async with database() as db, db.begin():
        mam = await db.get(SourceConnection, "mam")
        mam.lease_token = uuid4()
        mam.lease_until = datetime.now(UTC) + timedelta(minutes=1)
    downloader_http["status"] = 403
    await connection_health.run()
    health = (await client.get("/api/health/connections")).json()
    assert health["connections"][0]["status"] == "untested"
    assert health["connections"][1]["status"] == "authentication"


async def test_recovery_mode_skips_checks(client, admin, source_http, monkeypatch):
    from app.config import get_settings

    await configure(client)
    monkeypatch.setattr(get_settings(), "recovery_mode", True)
    await connection_health.run()
    # Read persisted data directly: normal APIs are blocked in recovery mode.
    monkeypatch.setattr(get_settings(), "recovery_mode", False)
    assert (await client.get("/api/sources/mam/connection")).json()["status"] == "untested"


async def test_summary_requires_authentication(client):
    assert (await client.get("/api/health/connections")).status_code == 401


async def test_working_egress_does_not_hide_mam_proxy_failure(client, admin, monkeypatch):
    from app.adapters.contracts import AdapterError, FailureKind
    from app.api import sources
    from app.domain import source_network

    class RoutedClient:
        rotated_cookie = None
        cooldown = None

        def __init__(self, *args, proxy_url=None, **kwargs):
            self.proxy = proxy_url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def test(self):
            if self.proxy:
                error = AdapterError(FailureKind.UNAVAILABLE, "Proxy cannot reach MAM")
                error.proxy_retryable = True
                raise error
            return {"uid": 99}

    async def probe(*args):
        return EgressResult(ip="203.0.113.1")

    monkeypatch.setattr(source_network, "MAMClient", RoutedClient)
    monkeypatch.setattr(sources, "probe_egress", probe)
    monkeypatch.setattr(connection_health, "probe_egress", probe)
    monkeypatch.setattr(source_network, "REQUEST_INTERVAL", 0)
    await configure(client, proxy_url="http://proxy.test:8888")
    await connection_health.run()
    result = (await client.get("/api/health/connections")).json()
    assert result["issues"] == 1
    assert result["connections"][0]["status"] == "connected"
    assert "direct fallback" in result["connections"][1]["message"]
    # Strict mode must retain the failed route even after a successful IP probe.
    await configure(
        client,
        proxy_url="http://proxy.test:8888",
        expected_generation=1,
        proxy_fallback_direct=False,
    )
    tested = (await client.post("/api/sources/mam/network/test")).json()
    assert tested["proxy_status"] == "unavailable"
    assert tested["connection"]["proxy_health"]["status"] == "unavailable"
    assert (await client.get("/api/health/connections")).json()["issues"] == 2
