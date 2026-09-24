import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.mam import MAMClient
from app.db.models import AuditEvent, SourceConnection, User
from app.domain import source_network
from app.security import decrypt_secrets
from tests.integration.test_correction_migration import migrate
from tests.mam_fixture import search_response

pytestmark = pytest.mark.integration


@pytest.fixture
def source_http(monkeypatch):
    state = {
        "calls": [],
        "cookie": "first-fixture",
        "status": 200,
        "headers": {},
        "body": None,
        "wait": None,
        "entered": asyncio.Event(),
    }

    async def handler(request):
        state["calls"].append(request)
        assert request.headers["cookie"] == "mam_id=" + state["cookie"]
        state["entered"].set()
        if state["wait"]:
            await state["wait"].wait()
        if state["body"] is not None:
            body = state["body"]
        elif request.url.path.endswith("jsonLoad.php"):
            body = {"uid": 99, "username": "private-tracker-user", "seedbonus": 123}
        else:
            body = search_response()
        state["cookie"] = "rotated-" + str(len(state["calls"]))
        return httpx.Response(
            state["status"],
            json=body,
            headers={"Set-Cookie": f"mam_id={state['cookie']}; Path=/", **state["headers"]},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        source_network,
        "MAMClient",
        lambda *args, **kwargs: MAMClient(*args, **kwargs, transport=transport),
    )
    monkeypatch.setattr(source_network, "REQUEST_INTERVAL", 0)
    return state


async def configure(client, **changes):
    value = {
        "base_url": "https://mam.test",
        "mam_id": "first-fixture",
        "expected_generation": 0,
        **changes,
    }
    return await client.put("/api/sources/mam/connection", json=value)


async def search(client, **changes):
    return await client.post("/api/sources/mam/search", json={"q": "Harbor", **changes})


async def test_connection_search_detail_rotation_and_redaction(
    client, admin, database, source_http
):
    configured = await configure(client)
    assert configured.status_code == 200, configured.text
    assert configured.json()["route"] == "direct"
    assert configured.json()["automation"]["use_wedge"] is False
    tested = await client.post("/api/sources/mam/connection/test")
    assert tested.status_code == 200 and tested.json()["status"] == "connected"
    found = await search(client)
    assert found.status_code == 200, found.text
    assert found.json()["items"][0]["source_id"] == "501"
    detailed = await client.get("/api/sources/mam/releases/501")
    assert detailed.status_code == 200 and detailed.json()["series"][0]["position"] == "1-3"
    serialized = configured.text + tested.text + found.text + detailed.text
    assert all(
        secret not in serialized
        for secret in [
            "first-fixture",
            "rotated-",
            "private-tracker-user",
            "fixture-private-download-token",
        ]
    )
    async with database() as db:
        row = await db.get(SourceConnection, "mam")
        assert "rotated-3" not in row.encrypted_secrets
        assert decrypt_secrets(row.encrypted_secrets)["mam_id"] == "rotated-3"
        assert row.lease_token is None and row.last_success_at
        audits = list(await db.scalars(select(AuditEvent)))
        assert "first-fixture" not in str([event.detail for event in audits])
    assert (await configure(client)).status_code == 409  # Optimistic settings edit.
    assert (await client.get("/api/integrations")).json() == []  # Not an ABS connection.


async def test_requests_serialize_and_reject_stale_connection_results(
    client, admin, database, source_http
):
    await configure(client)
    source_http["wait"] = asyncio.Event()
    task = asyncio.create_task(search(client))
    await asyncio.wait_for(source_http["entered"].wait(), 5)
    blocked = await search(client)
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "2"
    assert len(source_http["calls"]) == 1
    changed = await configure(client, expected_generation=1, mam_id="replacement-fixture")
    assert changed.status_code == 200
    source_http["wait"].set()
    stale = await task
    assert stale.status_code == 409
    async with database() as db:
        row = await db.get(SourceConnection, "mam")
        assert decrypt_secrets(row.encrypted_secrets)["mam_id"] == "replacement-fixture"
        assert row.lease_token is None and row.status == "untested"
    source_http["cookie"] = "replacement-fixture"
    assert (await search(client)).status_code == 200


async def test_actor_revocation_hides_results_but_preserves_rotated_shared_session(
    client, admin, database, source_http
):
    await configure(client)
    source_http["wait"] = asyncio.Event()
    task = asyncio.create_task(search(client))
    await asyncio.wait_for(source_http["entered"].wait(), 5)
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).active = False
    source_http["wait"].set()
    assert (await task).status_code == 401
    async with database() as db:
        row = await db.get(SourceConnection, "mam")
        assert decrypt_secrets(row.encrypted_secrets)["mam_id"] == "rotated-1"
        assert row.lease_token is None


async def test_source_cooldown_survives_configuration_edit_and_fresh_request(
    client, admin, database, source_http
):
    await configure(client)
    source_http.update(status=429, headers={"Retry-After": "120"})
    limited = await search(client)
    assert limited.status_code == 429
    assert (await configure(client, expected_generation=1, mam_id="new-fixture")).status_code == 200
    again = await search(client)
    assert again.status_code == 429 and len(source_http["calls"]) == 1
    assert int(again.headers["Retry-After"]) > 100
    async with database() as db:
        row = await db.get(SourceConnection, "mam")
        assert row.blocked_until > datetime.now(UTC) and row.lease_token is None


async def test_interrupted_session_requires_explicit_current_cookie(
    client, admin, database, source_http
):
    await configure(client)
    async with database() as db, db.begin():
        row = await db.get(SourceConnection, "mam")
        row.lease_token, row.lease_until = uuid4(), datetime.now(UTC) - timedelta(seconds=1)
    assert (await search(client)).status_code == 409
    assert not source_http["calls"]
    await configure(client, expected_generation=1, mam_id="current-fixture")
    source_http["cookie"] = "current-fixture"
    assert (await search(client)).status_code == 200


async def test_members_search_without_reading_or_changing_credentials(
    client, admin, database, source_http
):
    await configure(client)
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "member"
    assert (await client.get("/api/sources/mam/connection")).status_code == 403
    assert (await configure(client, expected_generation=1)).status_code == 403
    assert (await client.post("/api/sources/mam/connection/test")).status_code == 403
    assert (await search(client)).status_code == 200
    client.headers.pop("X-CSRF-Token")
    assert (await search(client)).status_code == 403


async def test_proxy_secrets_endpoint_changes_and_disabled_state(
    client, admin, database, source_http
):
    body = {
        "proxy_url": "http://gluetun:8888",
        "proxy_username": "proxy-user",
        "proxy_password": "proxy-secret",
    }
    configured = await configure(client, **body)
    assert configured.status_code == 200
    assert (
        configured.json()["route"] == "proxy-preferred"
        and configured.json()["has_proxy_credentials"]
    )
    assert "proxy-secret" not in configured.text and "proxy-user" not in configured.text
    assert (
        await configure(
            client, base_url="https://different.test", expected_generation=1, mam_id=None
        )
    ).status_code == 422
    changed = await configure(
        client,
        expected_generation=1,
        mam_id=None,
        proxy_url="http://other-proxy:8888",
        enabled=False,
    )
    assert changed.status_code == 200 and not changed.json()["has_proxy_credentials"]
    assert (await search(client)).status_code == 409
    assert not source_http["calls"]
    bad = await configure(client, expected_generation=2, mam_id="header\r\nsecret-leak")
    assert bad.status_code == 422 and "secret-leak" not in bad.text
    assert (await configure(client, expected_generation=2, base_url="")).status_code == 422


async def test_parser_failure_records_safe_error_and_persists_cookie(
    client, admin, database, source_http
):
    await configure(client)
    source_http["body"] = {"error": "unknown problem with credential-private-value"}
    response = await search(client)
    assert response.status_code == 502 and "credential-private-value" not in response.text
    async with database() as db:
        row = await db.get(SourceConnection, "mam")
        assert row.status == "parser" and "credential-private-value" not in row.last_error
        assert decrypt_secrets(row.encrypted_secrets)["mam_id"] == "rotated-1"


async def test_source_migration_preserves_credentials_by_refusing_lossy_downgrade(
    client, admin, database
):
    await configure(client)
    refused = await migrate("downgrade", "0014_import_cancel")
    assert refused.returncode != 0 and "Source credentials and session state" in refused.stderr
    async with database() as db, db.begin():
        await db.delete(await db.get(SourceConnection, "mam"))
    try:
        downgraded = await migrate("downgrade", "0014_import_cancel")
        assert downgraded.returncode == 0, downgraded.stderr
    finally:
        upgraded = await migrate("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr


async def test_disabling_during_search_keeps_same_session_rotation_but_hides_results(
    client, admin, database, source_http
):
    await configure(client)
    source_http["wait"] = asyncio.Event()
    task = asyncio.create_task(search(client))
    await asyncio.wait_for(source_http["entered"].wait(), 5)
    changed = await configure(client, expected_generation=1, mam_id=None, enabled=False)
    assert changed.status_code == 200
    source_http["wait"].set()
    assert (await task).status_code == 409
    async with database() as db:
        row = await db.get(SourceConnection, "mam")
        assert decrypt_secrets(row.encrypted_secrets)["mam_id"] == "rotated-1"
        assert not row.enabled and row.lease_token is None


async def test_cancelled_http_request_leaves_fenced_session_for_recovery(
    client, admin, database, source_http
):
    await configure(client)
    source_http["wait"] = asyncio.Event()
    task = asyncio.create_task(search(client))
    await asyncio.wait_for(source_http["entered"].wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with database() as db:
        row = await db.get(SourceConnection, "mam")
        assert row.lease_token and row.lease_until > datetime.now(UTC)
    assert (await search(client)).status_code == 429
    assert len(source_http["calls"]) == 1


async def test_network_diagnostics_cookie_rotation_routes_and_failures(
    client, admin, database, source_http, monkeypatch
):
    from app.api import sources
    from app.domain.mam_diagnostics import EgressResult

    probes = []

    async def probe(proxy=None, username=None, password=None):
        probes.append((proxy, username, password))
        return EgressResult(ip="203.0.113.1" if proxy else "198.51.100.2")

    monkeypatch.setattr(sources, "probe_egress", probe)
    await configure(
        client, proxy_url="http://proxy:8888", proxy_username="private", proxy_password="secret"
    )
    # MockTransport cannot override httpx's explicit proxy mount. Keep the real
    # session flow while the existing source fixture supplies MAM responses.
    original = source_network.MAMClient
    monkeypatch.setattr(
        source_network, "MAMClient", lambda *a, **kw: original(*a, **{**kw, "proxy_url": None})
    )
    result = await client.post("/api/sources/mam/network/test")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["status"] == "healthy" and data["cookie_status"] == "authenticated"
    assert data["proxy"]["ip"] == "203.0.113.1"
    assert data["direct"]["ip"] == "198.51.100.2"
    assert ("http://proxy:8888", "private", "secret") in probes
    assert "secret" not in result.text and "private" not in result.text
    source_http.update(status=401)
    failed = (await client.post("/api/sources/mam/network/test")).json()
    assert failed["status"] == "unhealthy" and failed["cookie_status"] == "rejected"
    assert failed["proxy_status"] == "healthy"
    assert failed["connection"]["status"] == "authentication"


async def test_network_diagnostics_disabled_and_member_access(client, admin, database):
    await configure(client, enabled=False)
    assert (await client.post("/api/sources/mam/network/test")).status_code == 409
    async with database() as db, db.begin():
        (await db.get(User, UUID(admin["id"]))).role = "member"
    assert (await client.post("/api/sources/mam/network/test")).status_code == 403


@pytest.mark.parametrize("proxy_available", [True, False])
async def test_network_only_skips_saved_cookie_and_preserves_account_status(
    client, admin, database, monkeypatch, proxy_available
):
    from app.api import sources
    from app.domain.mam_diagnostics import EgressResult

    async def probe(proxy=None, *args):
        if proxy and not proxy_available:
            return EgressResult(error="Proxy DNS failed")
        return EgressResult(ip="203.0.113.1" if proxy else "198.51.100.2")

    async def unexpected_account_request(*args, **kwargs):
        pytest.fail("A proxy-only check must not test the saved MAM cookie")

    monkeypatch.setattr(sources, "probe_egress", probe)
    monkeypatch.setattr(sources, "source_call", unexpected_account_request)
    await configure(client, proxy_url="http://gluetun:8888", proxy_fallback_direct=False)
    async with database() as db, db.begin():
        row = await db.get(SourceConnection, "mam")
        row.status, row.last_error = "authentication", "MAM rejected the session."
    response = await client.post("/api/sources/mam/network/test?include_cookie=false")
    assert response.status_code == 200
    data = response.json()
    assert data["cookie_status"] == "not-tested"
    assert data["status"] == ("healthy" if proxy_available else "unhealthy")
    assert data["proxy_status"] == ("healthy" if proxy_available else "unavailable")
    assert data["direct"]["ip"] == "198.51.100.2"
    assert data["connection"]["status"] == "authentication"
    assert data["connection"]["last_error"] == "MAM rejected the session."


async def test_proxy_setup_without_cookie_can_test_network_then_authenticate(
    client, admin, database, source_http, monkeypatch
):
    from app.api import sources
    from app.domain.mam_diagnostics import EgressResult

    probes = []

    async def probe(proxy=None, username=None, password=None):
        probes.append((proxy, username, password))
        return EgressResult(ip="203.0.113.1" if proxy else "198.51.100.2")

    monkeypatch.setattr(sources, "probe_egress", probe)
    configured = await configure(
        client,
        mam_id=None,
        proxy_url="http://gluetun:8888",
        proxy_username="private-user",
        proxy_password="private-password",
        proxy_fallback_direct=False,
    )
    assert configured.status_code == 200, configured.text
    assert not configured.json()["has_session"]
    result = await client.post("/api/sources/mam/network/test")
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["cookie_status"] == "not-configured"
    assert data["proxy_status"] == "healthy"
    assert data["status"] == "degraded"  # Network works; MAM access is still unverified.
    assert data["proxy"]["ip"] == "203.0.113.1"
    assert data["direct"]["ip"] == "198.51.100.2"
    assert ("http://gluetun:8888", "private-user", "private-password") in probes
    assert "private-" not in result.text
    assert not source_http["calls"]
    blocked = await search(client)
    assert blocked.status_code == 409 and "Enter mam_id" in blocked.text
    async with database() as db:
        row = await db.get(SourceConnection, "mam")
        assert row.lease_token is None and row.next_request_at is None
    # Supplying the cookie after network setup must still use the normal session flow.
    original = source_network.MAMClient
    monkeypatch.setattr(
        source_network, "MAMClient", lambda *a, **kw: original(*a, **{**kw, "proxy_url": None})
    )
    saved = await configure(client, expected_generation=1, proxy_url="http://gluetun:8888")
    assert saved.status_code == 200 and saved.json()["has_session"]
    authenticated = (await client.post("/api/sources/mam/network/test")).json()
    assert authenticated["cookie_status"] == "authenticated"
    assert authenticated["status"] == "healthy"


async def test_missing_cookie_does_not_mask_failed_proxy(client, admin, monkeypatch):
    from app.api import sources
    from app.domain.mam_diagnostics import EgressResult

    async def probe(proxy=None, *args):
        return EgressResult(error="Proxy DNS failed") if proxy else EgressResult(ip="198.51.100.2")

    monkeypatch.setattr(sources, "probe_egress", probe)
    await configure(client, mam_id=None, proxy_url="http://gluetun:8888")
    result = (await client.post("/api/sources/mam/network/test")).json()
    assert result["cookie_status"] == "not-configured"
    assert result["proxy_status"] == "unavailable"
    assert result["proxy"]["error"] == "Proxy DNS failed"
    assert result["status"] == "unhealthy"


async def test_network_ip_lookup_failure_does_not_reject_authenticated_cookie(
    client, admin, database, source_http, monkeypatch
):
    from app.api import sources
    from app.domain.mam_diagnostics import EgressResult

    async def unavailable(*args):
        return EgressResult(error="IP lookup unavailable")

    monkeypatch.setattr(sources, "probe_egress", unavailable)
    await configure(client)
    response = await client.post("/api/sources/mam/network/test")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"
    assert data["cookie_status"] == "authenticated"
    assert data["proxy"] is None and data["proxy_status"] == "not-configured"
    assert data["direct"]["ip"] is None
    assert data["connection"]["status"] == "connected"


async def test_unavailable_proxy_falls_back_direct_unless_strict_mode_is_enabled(
    client, admin, monkeypatch
):
    from app.api import sources
    from app.domain.mam_diagnostics import EgressResult

    attempts = []

    class RoutedClient:
        def __init__(self, *args, proxy_url=None, **kwargs):
            self.proxy_url = proxy_url
            self.rotated_cookie = None
            self.cooldown = 0
            self.automation = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def test(self):
            attempts.append(self.proxy_url)
            if self.proxy_url:
                error = AdapterError(FailureKind.ROUTE, "Proxy unavailable")
                error.proxy_retryable = True
                raise error

    async def probe(proxy=None, *args):
        return (
            EgressResult(error="Proxy unavailable") if proxy else EgressResult(ip="198.51.100.20")
        )

    monkeypatch.setattr(source_network, "MAMClient", RoutedClient)
    monkeypatch.setattr(sources, "probe_egress", probe)
    configured = await configure(client, proxy_url="http://proxy.test:8888")
    assert configured.json()["proxy_fallback_direct"] is True

    fallback = (await client.post("/api/sources/mam/network/test")).json()
    assert fallback["status"] == "degraded"
    assert fallback["route"] == "direct-fallback"
    assert fallback["cookie_status"] == "authenticated"
    assert attempts == ["http://proxy.test:8888", None]

    strict = await configure(
        client,
        expected_generation=1,
        mam_id=None,
        proxy_url="http://proxy.test:8888",
        proxy_fallback_direct=False,
    )
    assert strict.json()["route"] == "required-proxy"
    failed = (await client.post("/api/sources/mam/network/test")).json()
    assert failed["status"] == "unhealthy"
    assert failed["route"] == "proxy"
    assert attempts == ["http://proxy.test:8888", None, "http://proxy.test:8888"]
