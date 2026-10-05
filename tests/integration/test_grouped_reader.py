from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.adapters.catalog_types import BookData
from app.db.models import AuditEvent, WorkMetadataSource
from tests.integration.test_library_editions import edition
from tests.integration.test_work_merges import merge

pytestmark = pytest.mark.integration


def source(work, external_id):
    book = BookData(
        provider="hardcover", external_id=external_id, title=work.title, authors=work.authors
    )
    return WorkMetadataSource(
        work_id=work.id,
        provider="hardcover",
        external_id=external_id,
        accepted=True,
        snapshot=book.model_dump(mode="json"),
        fetched_at=datetime.now(UTC),
    )


async def test_reader_scope_combines_editions_but_editor_scope_keeps_origin(
    client, admin, database
):
    async with database() as db, db.begin():
        ebook, ev, _, _ = await edition(db, "One Book", "ebook")
        audio, av, _, _ = await edition(db, "One Book (Unabridged)", "audio", narrators=["Reader"])
        db.add(source(audio, "123"))
        ids = [str(ebook.id), str(audio.id)]
        versions = {str(ev.id), str(av.id)}
    for work_id in ids:
        detail = (await client.get(f"/api/catalog/works/{work_id}")).json()
        assert detail["id"] == work_id  # Display choices cannot relocate a bookmark.
        page = (
            await client.get(
                f"/api/metadata/works/{work_id}", params={"scope": "display", "limit": 1}
            )
        ).json()
        assert page["versions_total"] == 2
        assert page["sources"][0]["work_id"] == ids[1]
        assert page["sources"][0]["revision"] is None
        second = (
            await client.get(
                f"/api/metadata/works/{work_id}",
                params={"scope": "display", "limit": 1, "offset": 1},
            )
        ).json()
        assert {page["versions"][0]["id"], second["versions"][0]["id"]} == versions
        assert all(v["owned"] for v in page["versions"] + second["versions"])
        editor = (await client.get(f"/api/metadata/works/{work_id}")).json()
        assert editor["versions_total"] == 1
        assert len(editor["sources"]) == (work_id == ids[1])


async def test_separation_is_persistent_reversible_and_revision_checked(client, admin, database):
    async with database() as db, db.begin():
        first, _, _, _ = await edition(db, "One Book", "ebook")
        second, _, _, _ = await edition(db, "One Book", "audio")
        first_id, second_id = str(first.id), str(second.id)
    group = (await client.get(f"/api/catalog/works/{first_id}/grouping")).json()
    assert len(group["members"]) == 2
    member = next(m for m in group["members"] if m["work"]["id"] == second_id)
    payload = {"separate": True, "expected_revision": member["revision"]}
    path = f"/api/catalog/works/{second_id}/grouping"
    assert (await client.patch(path, json=payload)).status_code == 204
    assert (await client.get("/api/library/books")).json()["total"] == 2
    assert (await client.patch(path, json=payload)).status_code == 409
    group = (await client.get(path)).json()
    assert group["members"][0]["separate"]
    assert (
        await client.patch(
            path, json={"separate": False, "expected_revision": group["members"][0]["revision"]}
        )
    ).status_code == 204
    assert (await client.get("/api/library/books")).json()["total"] == 1
    async with database() as db:
        assert await db.scalar(
            select(AuditEvent.id).where(AuditEvent.action == "catalog.grouping.changed")
        )


@pytest.mark.parametrize("same_id", [True, False])
async def test_accepted_provider_conflicts_prevent_automatic_grouping(
    client, admin, database, same_id
):
    async with database() as db, db.begin():
        first, _, _, _ = await edition(db, "One Book", "ebook")
        second, _, _, _ = await edition(db, "One Book", "audio")
        db.add_all([source(first, "123"), source(second, "123" if same_id else "456")])
    assert (await client.get("/api/library/books")).json()["total"] == (1 if same_id else 2)


async def test_undo_confirmed_merge_does_not_automatically_regroup(client, admin, database):
    async with database() as db, db.begin():
        first, _, _, _ = await edition(db, "One Book", "ebook")
        second, _, _, _ = await edition(db, "One Book", "audio")
        first_id, second_id = str(first.id), str(second.id)
    change = await merge(client, second_id, first_id)
    assert (await client.post(f"/api/identity/changes/{change['id']}/undo")).status_code == 204
    assert (await client.get("/api/library/books")).json()["total"] == 2


async def test_request_hints_do_not_fulfill_unconfirmed_siblings(client, admin, database):
    async with database() as db, db.begin():
        ebook, _, _, _ = await edition(db, "One Book", "ebook")
        audio, version, _, _ = await edition(db, "One Book", "audio", narrators=["Reader"])
        version.language = "en"
        ebook_id, audio_id = str(ebook.id), str(audio.id)
    response = await client.post(
        "/api/requests/preview",
        json={"work_id": ebook_id, "specification": {"mode": "audio", "language": "en"}},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["targets"][0]["state"] == "wanted"
    assert data["existing_copies"][0]["work_id"] == audio_id
    assert data["existing_copies"][0]["meets_requirements"]
    response = await client.post(
        "/api/requests/preview",
        json={
            "work_id": ebook_id,
            "specification": {"mode": "audio", "required_narrators": ["Another Reader"]},
        },
    )
    assert response.status_code == 200, response.text
    assert not response.json()["existing_copies"][0]["meets_requirements"]


async def test_primary_edition_and_format_freshness(client, admin, database):
    async with database() as db, db.begin():
        ebook, _, _, _ = await edition(db, "One Book", "ebook")
        _, v1, a1, _ = await edition(db, "One Book", "audio", narrators=["First Reader"])
        _, v2, _, _ = await edition(db, "One Book", "audio", narrators=["Chosen Reader"])
        work_id, version_id, first_asset = str(ebook.id), str(v2.id), a1.id
    group = (await client.get(f"/api/catalog/works/{work_id}/grouping")).json()
    member = next(m for m in group["members"] if m["work"]["id"] == work_id)
    response = await client.patch(
        f"/api/catalog/works/{work_id}/primary-edition",
        json={"medium": "audio", "version_id": version_id, "expected_revision": member["revision"]},
    )
    assert response.status_code == 204, response.text
    data = (await client.get(f"/api/catalog/works/{work_id}")).json()["availability"]
    assert data["primary_audio_version_id"] == version_id
    assert data["primary_audio_selected"] is True
    assert data["primary_audio_narrators"] == ["Chosen Reader"]
    from app.db.models import LibraryAsset

    async with database() as db, db.begin():
        for asset in await db.scalars(select(LibraryAsset).where(LibraryAsset.medium == "audio")):
            asset.state = "stale"
    data = (await client.get(f"/api/catalog/works/{work_id}")).json()["availability"]
    assert data["audio_stale"] and not data["ebook_stale"]
    async with database() as db, db.begin():
        (await db.get(LibraryAsset, first_asset)).state = "present"
    data = (await client.get(f"/api/catalog/works/{work_id}")).json()["availability"]
    assert not data["audio_stale"]
    assert data["primary_audio_narrators"] == ["First Reader"]


async def test_private_group_members_and_editions_are_hidden(client, admin, database):
    from uuid import UUID

    from app.db.models import LibraryGrant
    from tests.integration.test_discovery import login_member

    async with database() as db, db.begin():
        ebook, _, _, library = await edition(db, "One Book", "ebook")
        audio, _, _, _ = await edition(db, "One Book", "audio", narrators=["Private Reader"])
        ebook.catalog_public = audio.catalog_public = False
        work_id, library_id = str(ebook.id), library.id
    user_id = await login_member(client)
    async with database() as db, db.begin():
        db.add(LibraryGrant(user_id=UUID(str(user_id)), library_id=library_id))
    metadata = (
        await client.get(f"/api/metadata/works/{work_id}", params={"scope": "display"})
    ).json()
    assert metadata["versions_total"] == 1
    group = (await client.get(f"/api/catalog/works/{work_id}/grouping")).json()
    assert len(group["members"]) == 1
    assert (
        await client.patch(
            f"/api/catalog/works/{work_id}/grouping",
            json={"separate": True, "expected_revision": group["members"][0]["revision"]},
        )
    ).status_code == 403
    hints = await client.post(
        "/api/requests/preview", json={"work_id": work_id, "specification": {"mode": "audio"}}
    )
    assert hints.status_code == 200, hints.text
    assert hints.json()["existing_copies"] == []


@pytest.mark.parametrize("language", ["zh-Hans", "sr-Latn"])
async def test_different_scripts_do_not_group(client, admin, database, language):
    async with database() as db, db.begin():
        await edition(db, "One Book", "ebook", language=language)
        await edition(
            db, "One Book", "audio", language="zh-Hant" if language == "zh-Hans" else "sr-Cyrl"
        )
    assert (await client.get("/api/library/books")).json()["total"] == 2


@pytest.mark.parametrize(
    "value", ["  Ｓａｌｅｍ’s   Lot (Unabridged)", "Cafe\u0301: A Novel", "ＪＯＪＯ  MOYES"]
)
async def test_python_sql_display_normalization_agree(database, value):
    from sqlalchemy import literal

    from app.domain.catalog_titles import display_title, display_title_sql

    async with database() as db:
        assert await db.scalar(select(display_title_sql(literal(value)))) == display_title(value)


async def test_primary_recording_drives_the_audio_cover(client, admin, database, monkeypatch):
    from app.db.models import Integration
    from app.domain import library_covers
    from app.security import encrypt_secrets

    async with database() as db, db.begin():
        first, _, a1, library1 = await edition(db, "One Book", "audio", narrators=["First Reader"])
        _, version, a2, library2 = await edition(
            db, "One Book", "audio", narrators=["Chosen Reader"]
        )
        for number, asset, library in [(1, a1, library1), (2, a2, library2)]:
            asset.created_at = datetime(2020 + number, 1, 1, tzinfo=UTC)
            asset.metadata_snapshot = {"cover_path": "cover.jpg"}
            integration = await db.get(Integration, library.integration_id)
            integration.base_url = f"http://cover-{number}.invalid"
            integration.encrypted_secrets = encrypt_secrets({"token": "fixture"})
        work_id, version_id = str(first.id), str(version.id)

    async def fetch(base, token, item, **_extra):
        return base.encode(), "image/jpeg"

    monkeypatch.setattr(library_covers, "fetch_cover", fetch)
    path = f"/api/catalog/works/{work_id}"
    assert (await client.get(path + "/cover?medium=audio")).content == b"http://cover-1.invalid"
    group = (await client.get(path + "/grouping")).json()
    member = next(m for m in group["members"] if m["work"]["id"] == work_id)
    assert (
        await client.patch(
            path + "/primary-edition",
            json={
                "medium": "audio",
                "version_id": version_id,
                "expected_revision": member["revision"],
            },
        )
    ).status_code == 204
    assert (await client.get(path + "/cover?medium=audio")).content == b"http://cover-2.invalid"
    assert (await client.get(path)).json()["availability"]["primary_audio_narrators"] == [
        "Chosen Reader"
    ]


async def test_cannot_choose_an_unrelated_primary_edition(client, admin, database):
    async with database() as db, db.begin():
        work, _, _, _ = await edition(db, "One Book", "audio")
        _, other, _, _ = await edition(db, "Another Book", "audio")
        work_id, version_id = str(work.id), str(other.id)
    path = f"/api/catalog/works/{work_id}"
    group = (await client.get(path + "/grouping")).json()
    response = await client.patch(
        path + "/primary-edition",
        json={
            "medium": "audio",
            "version_id": version_id,
            "expected_revision": group["members"][0]["revision"],
        },
    )
    assert response.status_code == 422
