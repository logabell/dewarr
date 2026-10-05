"""Account lifecycle edits preserve ownership and protect administrator access."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.db.models import AuditEvent, DiscoveryFollow, OidcIdentity, PlexIdentity, User
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


async def removable_account(client):
    response = await client.post(
        "/api/auth/users",
        json={
            "username": "removable",
            "display_name": "Removable",
            "password": "a long removable password",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def removal_body(client, user):
    methods = await client.get(f"/api/auth/users/{user['id']}/sign-in")
    assert methods.status_code == 200, methods.text
    return {
        "confirm_username": user["username"],
        "expected_revision": methods.json()["revision"],
    }


async def test_delete_unused_account_revokes_sessions_and_releases_username(
    client, admin, database
):
    user = await removable_account(client)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as reader:
        login = await reader.post(
            "/api/auth/login",
            json={"username": user["username"], "password": "a long removable password"},
        )
        assert login.status_code == 200
        reader.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        url = f"/api/auth/users/{user['id']}"
        body = await removal_body(client, user)
        assert (await reader.request("DELETE", url, json=body)).status_code == 403
        assert (
            await client.request("DELETE", url, json={**body, "confirm_username": "wrong"})
        ).status_code == 409
        response = await client.request("DELETE", url, json=body)
        assert response.status_code == 204, response.text
        assert (await reader.get("/api/auth/me")).status_code == 401
    async with database() as db:
        assert await db.get(User, UUID(user["id"])) is None
        event = await db.scalar(select(AuditEvent).where(AuditEvent.action == "user.deleted"))
        assert event.entity_id == UUID(user["id"]) and event.actor_id == UUID(admin["id"])
    replacement = await removable_account(client)
    assert replacement["id"] != user["id"]


@pytest.mark.parametrize("history", ["audit", "followed-list"])
async def test_account_history_and_last_administrator_cannot_be_deleted(
    client, admin, database, history
):
    user = await removable_account(client)
    async with database() as db, db.begin():
        db.add(
            AuditEvent(actor_id=UUID(user["id"]), action="fixture.activity")
            if history == "audit"
            else DiscoveryFollow(
                user_id=UUID(user["id"]),
                collection_id="fixture-list",
                snapshot={"title": "Saved reading list"},
                next_check_at=datetime.now(UTC),
            )
        )
    response = await client.request(
        "DELETE", f"/api/auth/users/{user['id']}", json=await removal_body(client, user)
    )
    assert response.status_code == 409, response.text
    assert "retained history" in response.json()["detail"]
    async with database() as db:
        assert (await db.get(User, UUID(user["id"]))).active
        if history == "audit":
            assert await db.scalar(
                select(AuditEvent.id).where(AuditEvent.actor_id == UUID(user["id"]))
            )
        else:
            assert await db.get(DiscoveryFollow, (UUID(user["id"]), "fixture-list"))
    response = await client.request(
        "DELETE", f"/api/auth/users/{admin['id']}", json=await removal_body(client, admin)
    )
    assert response.status_code == 409
    assert "at least one" in response.json()["detail"]


@pytest.mark.parametrize("provider", ["oidc", "plex"])
async def test_admin_unlink_requires_alternate_login_and_preserves_account(
    client, admin, database, provider
):
    user = await removable_account(client)
    identifier = UUID(user["id"])
    model = OidcIdentity if provider == "oidc" else PlexIdentity
    async with database() as db, db.begin():
        account = await db.get(User, identifier)
        password = account.password_hash
        account.password_hash = None
        db.add(
            OidcIdentity(user_id=identifier, issuer="https://identity.test", subject="reader")
            if provider == "oidc"
            else PlexIdentity(user_id=identifier, plex_user_id="42")
        )
    url = f"/api/auth/users/{identifier}/providers/{provider}"
    body = await removal_body(client, user)
    response = await client.request("DELETE", url, json=body)
    assert response.status_code == 409
    assert "another enabled" in response.json()["detail"]
    async with database() as db, db.begin():
        assert await db.get(model, identifier)
        (await db.get(User, identifier)).password_hash = password
    assert (await client.request("DELETE", url, json=body)).status_code == 409
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as reader:
        assert (
            await reader.post(
                "/api/auth/login",
                json={"username": user["username"], "password": "a long removable password"},
            )
        ).status_code == 200
        body = await removal_body(client, user)
        response = await client.request("DELETE", url, json=body)
        assert response.status_code == 200, response.text
        assert response.json()[provider] is False and response.json()["local_password"]
        assert (await reader.get("/api/auth/me")).status_code == 401
        assert (
            await reader.post(
                "/api/auth/login",
                json={"username": user["username"], "password": "a long removable password"},
            )
        ).status_code == 200
    async with database() as db:
        assert await db.get(model, identifier) is None
        assert (await db.get(User, identifier)).username == user["username"]
