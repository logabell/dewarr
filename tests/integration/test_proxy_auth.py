"""TLS-terminating proxy authentication and PostgreSQL limiter regressions."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.auth import enforce_auth_budget
from app.config import get_settings
from app.db.models import RateLimit
from app.main import create_app

pytestmark = pytest.mark.integration


class HTTPUpstream:
    def __init__(self, app, rewrite_host=False):
        self.app = app
        self.rewrite_host = rewrite_host

    async def __call__(self, scope, receive, send):
        scope = {**scope, "scheme": "http"}
        if self.rewrite_host:
            scope["headers"] = [
                (name, b"dewarr:8000" if name == b"host" else value)
                for name, value in scope["headers"]
            ]
        await self.app(scope, receive, send)


@pytest.mark.parametrize("rewrite_host", [False, True])
async def test_https_browser_session_through_http_upstream(database, monkeypatch, rewrite_host):
    settings = get_settings()
    monkeypatch.setattr(settings, "public_url", "https://books.example.com:8443")
    monkeypatch.setattr(settings, "public_origin", ("https", "books.example.com", 8443))
    monkeypatch.setattr(settings, "cookie_secure", True)
    transport = httpx.ASGITransport(app=HTTPUpstream(create_app(), rewrite_host))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://books.example.com:8443",
        headers={"Origin": "https://books.example.com:8443"},
    ) as browser:
        credentials = {"username": "proxy-admin", "password": "a long test password"}
        response = await browser.post(
            "/api/auth/bootstrap", json={**credentials, "display_name": "Proxy admin"}
        )
        assert response.status_code == 201, response.text
        assert "Secure" in response.headers["set-cookie"]
        assert (await browser.get("/api/auth/me")).status_code == 200
        browser.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        rejected = await browser.post(
            "/api/auth/logout", headers={"Origin": "https://evil.example"}
        )
        assert rejected.status_code == 403
        assert "X-Request-ID" in rejected.headers
        assert (await browser.post("/api/auth/logout")).status_code == 204
        assert (await browser.get("/api/auth/me")).status_code == 401
        response = await browser.post("/api/auth/login", json=credentials)
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        assert (await browser.get("/api/auth/me")).status_code == 200


async def test_trusted_proxy_separates_clients_and_keeps_account_limit(client, admin, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "proxy_token", None)
    from ipaddress import ip_network

    monkeypatch.setattr(settings, "proxy_networks", (ip_network("127.0.0.1"),))
    headers = {"X-Forwarded-For": "198.51.100.1"}
    # Different usernames ensure the first client's IP budget is what is exhausted.
    for index in range(15):
        response = await client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": f"unknown-{index}", "password": "a wrong long password"},
        )
        assert response.status_code == 401
    response = await client.post(
        "/api/auth/login",
        headers=headers,
        json={"username": "admin", "password": "a long test password"},
    )
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 600
    # A spoofed prefix cannot change the exhausted client's address.
    response = await client.post(
        "/api/auth/login",
        headers={"X-Forwarded-For": "1.1.1.1, 198.51.100.1"},
        json={"username": "admin", "password": "a long test password"},
    )
    assert response.status_code == 429
    response = await client.post(
        "/api/auth/login",
        headers={"X-Forwarded-For": "198.51.100.2"},
        json={"username": "admin", "password": "a long test password"},
    )
    assert response.status_code == 200
    # Rotating trusted client IPs must not bypass the independent account budget.
    for index in range(15):
        response = await client.post(
            "/api/auth/login",
            headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
            json={"username": "target-account", "password": "a wrong long password"},
        )
        assert response.status_code == 401
    response = await client.post(
        "/api/auth/login",
        headers={"X-Forwarded-For": "203.0.113.20"},
        json={"username": "target-account", "password": "a wrong long password"},
    )
    assert response.status_code == 429


async def test_auth_budget_atomic_under_concurrency_and_resets(database):
    async def attempt():
        async with database() as db:
            try:
                await enforce_auth_budget(db, "ip:concurrency")
                return 200
            except HTTPException as error:
                return error.status_code

    results = await asyncio.gather(*(attempt() for _ in range(20)))
    assert results.count(200) == 15
    assert results.count(429) == 5
    async with database() as db:
        row = await db.scalar(select(RateLimit).where(RateLimit.key == "ip:concurrency"))
        assert row.count == 20
        row.resets_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await attempt() == 200
    async with database() as db:
        row = await db.get(RateLimit, "ip:concurrency")
        assert row.count == 1
        assert row.resets_at > datetime.now(UTC)
