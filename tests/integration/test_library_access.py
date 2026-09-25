# ruff: noqa: F401, F811
"""Normal library grants unblock members without widening their authority."""

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Integration, Library, LibraryGrant, User
from tests.integration.test_acquisition import catalog
from tests.integration.test_plex import finish_sign_in, plex

pytestmark = pytest.mark.integration


async def test_plex_member_inherited_library_access_can_be_granted_and_revoked(
    client, admin, database, catalog, plex
):
    library_id = str(catalog["library"])
    work_id = str(catalog["work"])
    grants_url = f"/api/library/libraries/{library_id}/grants"
    await client.get("/api/auth/plex/link")
    await client.get("/api/auth/plex/callback")
    settings = await client.put(
        "/api/auth/plex/settings",
        json={
            "enabled": True,
            "machine_id": "abcdef1234",
            "auto_register": True,
            "default_role": "member",
        },
    )
    assert settings.status_code == 200, settings.text
    before = (await client.get("/api/acquisition/preferences/installation")).json()
    saved = await client.put(
        "/api/acquisition/preferences/installation",
        json={
            "overrides": {"desired_media": "ebook", "ebook_library_id": library_id},
            "expected_revision": before["revision"],
        },
    )
    assert saved.status_code == 200, saved.text
    # A separate browser session retains the admin's CSRF/session credentials.
    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as member:
        await member.get("/api/auth/plex/start")
        await finish_sign_in(member, await member.get("/api/auth/plex/callback"))
        me = (await member.get("/api/auth/me")).json()
        member.headers["X-CSRF-Token"] = me["csrf_token"]
        member_id = me["user"]["id"]
        assert me["user"]["role"] == "member"

        async def request():
            return await member.post(
                "/api/requests",
                json={"work_id": work_id, "specification": {}},
                headers={"Idempotency-Key": str(uuid4())},
            )

        assert (await member.get("/api/library/libraries")).json() == []
        assert (await member.get("/api/discovery/library")).json()["items"] == []
        denied = await request()
        assert denied.status_code == 404, denied.text
        assert "Settings → Libraries" in denied.json()["detail"]
        denied = await member.put(grants_url, json={"user_ids": [member_id]})
        assert denied.status_code == 403, denied.text

        granted = await client.put(
            grants_url, json={"user_ids": [member_id], "expected_user_ids": []}
        )
        assert granted.status_code == 204, granted.text
        libraries = (await member.get("/api/library/libraries")).json()
        assert [library["id"] for library in libraries] == [library_id]
        assert libraries[0]["granted_user_ids"] == []  # Other accounts stay private.
        shelf = (await member.get("/api/discovery/library")).json()
        assert [item["work"]["id"] for item in shelf["items"]] == [work_id]
        assert shelf["items"][0]["work"]["availability"]["ebook"]
        accepted = await request()
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["request"]["specification"]["ebook_library_id"] == library_id

        revoked = await client.put(
            grants_url, json={"user_ids": [], "expected_user_ids": [member_id]}
        )
        assert revoked.status_code == 204, revoked.text
        assert (await member.get("/api/library/libraries")).json() == []
        assert (await member.get("/api/discovery/library")).json()["items"] == []
        denied = await request()
        assert denied.status_code == 404, denied.text
        assert "Settings → Libraries" in denied.json()["detail"]
    # Administrator visibility is unaffected by an empty grant set.
    assert len((await client.get("/api/library/libraries")).json()) == 1


async def test_grant_edits_detect_conflicts_and_preserve_disabled_existing_accounts(
    client, admin, database, catalog
):
    ids = []
    for name in ("first", "second", "disabled"):
        response = await client.post(
            "/api/auth/users",
            json={"username": name, "display_name": name, "password": "a long test password"},
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    grants_url = f"/api/library/libraries/{catalog['library']}/grants"
    attempts = await asyncio.gather(
        *[
            client.put(grants_url, json={"user_ids": [user_id], "expected_user_ids": []})
            for user_id in ids[:2]
        ]
    )
    assert sorted(response.status_code for response in attempts) == [204, 409]
    library = (await client.get("/api/library/libraries")).json()[0]
    winner = library["granted_user_ids"][0]
    other = next(user_id for user_id in ids[:2] if user_id != winner)
    async with database() as db, db.begin():
        for user_id in (winner, ids[2]):
            (await db.get(User, UUID(user_id))).active = False
    accounts = (await client.get("/api/auth/users")).json()
    assert {user["id"] for user in accounts if not user["active"]} == {winner, ids[2]}
    retained = await client.put(
        grants_url,
        json={"user_ids": [winner, other], "expected_user_ids": [winner]},
    )
    assert retained.status_code == 204, retained.text
    for invalid_id in (ids[2], str(uuid4())):
        invalid = await client.put(grants_url, json={"user_ids": [winner, other, invalid_id]})
        assert invalid.status_code == 422, invalid.text
    library = (await client.get("/api/library/libraries")).json()[0]
    assert set(library["granted_user_ids"]) == {winner, other}
    revoked = await client.put(
        grants_url, json={"user_ids": [other], "expected_user_ids": [winner, other]}
    )
    assert revoked.status_code == 204, revoked.text
    assert (await client.get("/api/library/libraries")).json()[0]["granted_user_ids"] == [other]


async def test_user_setup_saves_named_role_and_libraries_atomically(
    client, admin, database, catalog
):
    library_id = str(catalog["library"])
    role = await client.post(
        "/api/auth/roles",
        json={"name": "Readers", "permissions": ["request", "request_ebook"]},
    )
    assert role.status_code == 201, role.text
    body = {
        "username": "new-reader",
        "display_name": "New reader",
        "password": "a long test password",
        "role_id": role.json()["id"],
        "library_ids": [str(uuid4())],
    }
    failed = await client.post("/api/auth/users", json=body)
    assert failed.status_code == 422, failed.text
    async with database() as db:
        assert not await db.scalar(select(User.id).where(User.username == "new-reader"))
    created = await client.post("/api/auth/users", json={**body, "library_ids": [library_id]})
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["permission_role_id"] == role.json()["id"]
    assert user["access_label"] == "Readers"
    assert user["library_ids"] == [library_id]
    listing = (await client.get("/api/auth/users")).json()
    assert next(item for item in listing if item["id"] == user["id"])["library_ids"] == [library_id]
    # Invalid library choices must also roll back a role change on an existing user.
    edit = {
        "permissions": [],
        "role_id": None,
        "expected_role_id": role.json()["id"],
        "expected_permissions": user["permissions"],
        "library_ids": [str(uuid4())],
        "expected_library_ids": [library_id],
    }
    failed = await client.put(f"/api/auth/users/{user['id']}/permissions", json=edit)
    assert failed.status_code == 422, failed.text
    async with database() as db:
        saved = await db.get(User, UUID(user["id"]))
        assert str(saved.permission_role_id) == role.json()["id"]
        assert (
            await db.scalar(select(LibraryGrant.library_id).where(LibraryGrant.user_id == saved.id))
            == catalog["library"]
        )
    edited = await client.put(
        f"/api/auth/users/{user['id']}/permissions", json={**edit, "library_ids": []}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["role"] == "viewer" and edited.json()["library_ids"] == []
    # Two roles can have identical permissions; the role identity still matters.
    alternate = await client.post(
        "/api/auth/roles",
        json={"name": "Other readers", "permissions": user["permissions"]},
    )
    assert alternate.status_code == 201, alternate.text
    changed = await client.put(
        f"/api/auth/users/{user['id']}/permissions",
        json={
            "permissions": user["permissions"],
            "role_id": alternate.json()["id"],
            "expected_permissions": [],
            "expected_role_id": None,
        },
    )
    assert changed.status_code == 200, changed.text
    stale = await client.put(
        f"/api/auth/users/{user['id']}/permissions",
        json={**edit, "library_ids": [library_id], "expected_library_ids": []},
    )
    assert stale.status_code == 409, stale.text
    current = next(
        item for item in (await client.get("/api/auth/users")).json() if item["id"] == user["id"]
    )
    assert current["permission_role_id"] == alternate.json()["id"]
    assert current["library_ids"] == []


async def test_user_and_library_editors_share_conflict_detection(client, admin, catalog):
    created = await client.post(
        "/api/auth/users",
        json={
            "username": "reader",
            "display_name": "Reader",
            "password": "a long test password",
        },
    )
    user = created.json()
    library_id = str(catalog["library"])
    edit = {
        "permissions": [],
        "expected_permissions": user["permissions"],
        "library_ids": [library_id],
        "expected_library_ids": [],
    }
    results = await asyncio.gather(
        client.put(f"/api/auth/users/{user['id']}/permissions", json=edit),
        client.put(
            f"/api/library/libraries/{library_id}/grants",
            json={
                "user_ids": [user["id"]],
                "expected_user_ids": [],
            },
        ),
    )
    assert sum(response.status_code == 409 for response in results) == 1
    assert any(response.status_code in {200, 204} for response in results)
    current = next(
        item for item in (await client.get("/api/auth/users")).json() if item["id"] == user["id"]
    )
    assert current["library_ids"] == [library_id]
    if results[0].status_code == 409:
        assert current["permissions"] == user["permissions"]


async def test_user_library_editor_preserves_disabled_state_and_shows_unavailable_libraries(
    client, admin, database, catalog
):
    library_id = str(catalog["library"])
    response = await client.post(
        "/api/auth/users",
        json={
            "username": "reader",
            "display_name": "Reader",
            "password": "a long test password",
            "library_ids": [library_id],
        },
    )
    user = response.json()
    async with database() as db, db.begin():
        saved = await db.get(User, UUID(user["id"]))
        saved.active = False
        original_permissions = saved.permissions
        library = await db.get(Library, catalog["library"])
        (await db.get(Integration, library.integration_id)).enabled = False
    assert (await client.get("/api/library/libraries")).json() == []
    choices = (await client.get("/api/library/libraries?include_disabled=true")).json()
    assert choices[0]["id"] == library_id and not choices[0]["accessible"]
    updated = await client.put(
        f"/api/auth/users/{user['id']}/permissions",
        json={
            "permissions": [],
            "expected_permissions": [],
            "library_ids": [],
            "expected_library_ids": [library_id],
        },
    )
    assert updated.status_code == 200, updated.text
    async with database() as db:
        saved = await db.get(User, UUID(user["id"]))
        assert saved.permissions == original_permissions
        assert saved.role == "member" and not saved.active


async def test_user_managers_cannot_assign_or_read_private_library_grants(client, admin, catalog):
    library_id = str(catalog["library"])
    created = await client.post(
        "/api/auth/users",
        json={
            "username": "manager",
            "display_name": "Manager",
            "password": "a long test password",
            "permissions": ["manage_users"],
        },
    )
    manager = created.json()
    logged_in = await client.post(
        "/api/auth/login", json={"username": "manager", "password": "a long test password"}
    )
    assert logged_in.status_code == 200, logged_in.text
    client.headers["X-CSRF-Token"] = logged_in.json()["csrf_token"]
    users = await client.get("/api/auth/users")
    assert users.status_code == 200
    assert all(item["library_ids"] is None for item in users.json())
    assert (await client.get("/api/library/libraries?include_disabled=true")).status_code == 403
    denied = await client.put(
        f"/api/auth/users/{manager['id']}/permissions",
        json={
            "permissions": manager["permissions"],
            "expected_permissions": manager["permissions"],
            "library_ids": [library_id],
            "expected_library_ids": [],
        },
    )
    assert denied.status_code == 403, denied.text
    denied = await client.post(
        "/api/auth/users",
        json={
            "username": "viewer",
            "display_name": "Viewer",
            "password": "a long test password",
            "role": "viewer",
            "library_ids": [library_id],
        },
    )
    assert denied.status_code == 403, denied.text
