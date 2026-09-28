"""Account lifecycle edits preserve ownership and protect administrator access."""

import httpx
import pytest

from app.main import create_app

pytestmark = pytest.mark.integration


def profile(user, **changes):
    return {
        "username": user["username"],
        "display_name": user["display_name"],
        "active": user["active"],
        "expected_username": user["username"],
        "expected_display_name": user["display_name"],
        "expected_active": user["active"],
        **changes,
    }


async def test_rename_and_disable_preserve_account_and_revoke_sessions(client, admin):
    created = await client.post(
        "/api/auth/users",
        json={
            "username": "reader",
            "display_name": "Reader",
            "password": "a long reader password",
        },
    )
    user = created.json()
    url = f"/api/auth/users/{user['id']}/profile"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as reader:
        login = await reader.post(
            "/api/auth/login", json={"username": "reader", "password": "a long reader password"}
        )
        reader.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert (await reader.put(url, json=profile(user, username="stolen"))).status_code == 403
        assert (await client.put(url, json=profile(user, username="ADMIN"))).status_code == 409
        renamed = await client.put(
            url, json=profile(user, username="Reader.Renamed", display_name="Renamed")
        )
        assert renamed.status_code == 200, renamed.text
        changed = renamed.json()
        assert changed["username"] == "reader.renamed"
        assert changed["id"] == user["id"]
        assert changed["permissions"] == user["permissions"]
        assert (await reader.get("/api/auth/me")).json()["user"]["username"] == "reader.renamed"
        assert (await client.put(url, json=profile(user))).status_code == 409
        disabled = await client.put(url, json=profile(changed, active=False))
        assert disabled.status_code == 200
        assert (await reader.get("/api/auth/me")).status_code == 401
        assert (
            await reader.post(
                "/api/auth/login",
                json={"username": "reader.renamed", "password": "a long reader password"},
            )
        ).status_code == 401
        enabled = await client.put(url, json=profile(disabled.json(), active=True))
        assert enabled.status_code == 200
        assert enabled.json()["permissions"] == user["permissions"]
        assert (await reader.get("/api/auth/me")).status_code == 401
        assert (
            await reader.post(
                "/api/auth/login",
                json={"username": "reader.renamed", "password": "a long reader password"},
            )
        ).status_code == 200


async def test_cannot_disable_self_or_last_administrator(client, admin):
    url = f"/api/auth/users/{admin['id']}/profile"
    denied = await client.put(url, json=profile(admin, active=False))
    assert denied.status_code == 409
    assert "at least one" in denied.json()["detail"]
    await client.post(
        "/api/auth/users",
        json={
            "username": "second",
            "display_name": "Second",
            "password": "a long second password",
            "role": "admin",
        },
    )
    denied = await client.put(url, json=profile(admin, active=False))
    assert denied.status_code == 409
    assert "another administrator" in denied.json()["detail"]
