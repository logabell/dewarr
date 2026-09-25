from uuid import UUID

import pytest
from sqlalchemy import delete, select

from app.db.models import Integration, LibraryAsset, LibraryGrant
from app.security import encrypt_secrets
from tests.integration.test_discovery import add_owned, add_work, login_member

pytestmark = pytest.mark.integration


async def test_covers_require_library_access_and_keep_credentials_server_side(
    client, admin, database, monkeypatch
):
    from app.domain import library_covers

    calls = []

    async def fetch(base_url, token, item_id, **_extra):
        calls.append((base_url, token, item_id))
        return b"image fixture", "image/jpeg"

    monkeypatch.setattr(library_covers, "fetch_cover", fetch)
    async with database() as db:
        work = await add_work(db)
        library = await add_owned(db, work)
        asset = await db.scalar(select(LibraryAsset))
        asset.metadata_snapshot = {"cover_path": "/private/cover.jpg"}
        integration = await db.get(Integration, library.integration_id)
        integration.encrypted_secrets = encrypt_secrets({"token": "private-token"})
        await db.commit()
        work_id, library_id = work.id, library.id
    url = f"/api/catalog/works/{work_id}/cover?medium=audio"
    response = await client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"]
    again = await client.get(url, headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304 and not again.content
    assert calls == [("http://fixture.invalid", "private-token", "one")]
    assert "private-token" not in response.text
    member = await login_member(client)
    calls.clear()
    assert (await client.get(url)).status_code == 404
    assert not calls
    async with database() as db:
        db.add(LibraryGrant(user_id=UUID(str(member)), library_id=library_id))
        await db.commit()
    assert (await client.get(url)).status_code == 200
    assert not calls  # Authorized readers share bytes, but still recheck grants.
    async with database() as db, db.begin():
        await db.execute(delete(LibraryGrant))
    assert (
        await client.get(url, headers={"If-None-Match": response.headers["etag"]})
    ).status_code == 404
    assert not calls
    async with database() as db, db.begin():
        db.add(LibraryGrant(user_id=member, library_id=library_id))
        asset = await db.scalar(select(LibraryAsset))
        asset.metadata_snapshot = {"cover_path": "/private/new-cover.jpg"}
    assert (await client.get(url)).status_code == 200
    assert len(calls) == 1
    async with database() as db, db.begin():
        integration = await db.scalar(select(Integration))
        integration.credential_generation += 1

    async def revoked_during_fetch(*_args, **_kwargs):
        async with database() as db, db.begin():
            await db.execute(delete(LibraryGrant))
        return b"revoked image", "image/jpeg"

    monkeypatch.setattr(library_covers, "fetch_cover", revoked_during_fetch)
    response = await client.get(url)
    assert response.status_code == 404 and b"revoked image" not in response.content
    await client.post("/api/auth/logout")
    assert (await client.get(url)).status_code == 401
