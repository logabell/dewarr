"""Known ebook copies retain their edition through ordinary backend inventory."""

import hashlib

import pytest
from sqlalchemy import func, select

from app.adapters.audiobookshelf import ABSFile, ABSItem
from app.config import get_settings
from app.db.models import (
    AssetContains,
    EbookCompanion,
    ImportDestination,
    Integration,
    Library,
    LibraryAsset,
    ProviderObject,
    Version,
    Work,
)
from app.domain.inventory import apply_item
from app.importing.destinations import destination_configuration
from app.importing.filesystem import identity
from app.security import encrypt_secrets

pytestmark = pytest.mark.integration


@pytest.fixture
async def companion_library(database, tmp_path, monkeypatch):
    root = tmp_path.resolve() / "library"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "import_destinations", {"audio": root})
    async with database() as db, db.begin():
        integration = Integration(
            kind="audiobookshelf",
            name="Books",
            base_url="http://abs.test",
            encrypted_secrets=encrypt_secrets({"token": "fixture"}),
        )
        work = Work(title="Harbor", authors=["Alex Morgan"])
        db.add_all([integration, work])
        await db.flush()
        library = Library(integration_id=integration.id, external_id="books", name="Books")
        ebook = Version(work_id=work.id, medium="ebook")
        db.add_all([library, ebook])
        await db.flush()
        source = LibraryAsset(
            library_id=library.id,
            external_id="canonical",
            medium="ebook",
            version_id=ebook.id,
            full_content=True,
            state="present",
            match_status="matched",
        )
        route = ImportDestination(
            root_key="audio",
            library_id=library.id,
            medium="audio",
            backend_path="/books",
            enabled=True,
        )
        db.add_all([source, route])
        await db.flush()
        configuration = await destination_configuration(db, route)
        items, copies = [], []
        for number in (1, 2):
            folder = root / f"narration-{number}"
            folder.mkdir()
            copy = folder / "Harbor.epub"
            copy.write_bytes(b"complete synthetic ebook")
            audio = ABSFile(path=f"/books/{folder.name}/Harbor.m4b", size=20, format="m4b")
            file = ABSFile(
                path=f"/books/{folder.name}/Harbor.epub",
                size=copy.stat().st_size,
                format="epub",
                inode="remote-inode",
                modified=1234,
            )
            item = ABSItem(
                id=f"audio-{number}",
                library_id="books",
                title="Harbor",
                authors=["Alex Morgan"],
                narrators=[f"Reader {number}"],
                path=f"/books/{folder.name}",
                audio=[audio],
                ebook=[file],
                library_files=[audio, file],
                full_audio=True,
                full_ebook=False,
                ebook_supplementary=True,
            )
            version = Version(work_id=work.id, medium="audio", narrators=item.narrators)
            db.add(version)
            await db.flush()
            target = LibraryAsset(
                library_id=library.id,
                external_id=item.id,
                medium="audio",
                version_id=version.id,
                state="present",
                full_content=True,
                match_status="matched",
                files=[audio.model_dump()],
                metadata_snapshot=item.model_dump(mode="json"),
            )
            db.add(target)
            db.add(
                ProviderObject(
                    provider=f"abs:{integration.id}",
                    kind="item:audio",
                    external_id=item.id,
                    work_id=work.id,
                    version_id=version.id,
                    manual_lock=True,
                    match_status="matched",
                    snapshot=item.model_dump(mode="json"),
                )
            )
            await db.flush()
            db.add(
                EbookCompanion(
                    library_id=library.id,
                    source_asset_id=source.id,
                    target_asset_id=target.id,
                    version_id=ebook.id,
                    source_path="/books/canonical/Harbor.epub",
                    target_path=file.path,
                    configuration={
                        "target_destination_id": str(route.id),
                        "target": configuration,
                        "target_relative": f"{folder.name}/Harbor.epub",
                    },
                    receipt={
                        **identity(copy.stat()),
                        "sha256": hashlib.sha256(copy.read_bytes()).hexdigest(),
                    },
                    state="present",
                )
            )
            items.append(item)
            copies.append(copy)
        return {
            "library_id": library.id,
            "ebook_id": ebook.id,
            "work_id": work.id,
            "source_id": source.id,
            "route_id": route.id,
            "items": items,
            "copies": copies,
        }


async def refresh(database, fixture, item):
    async with database() as db, db.begin():
        library = await db.get(Library, fixture["library_id"])
        await apply_item(db, library, item, 2, library.integration_id, {item.id})
        await db.flush()
        asset = await db.scalar(
            select(LibraryAsset).where(
                LibraryAsset.library_id == library.id,
                LibraryAsset.external_id == item.id,
                LibraryAsset.medium == "ebook",
            )
        )
        return asset.id


async def test_supplementary_copies_share_ebook_identity_and_survive_source_loss(
    database, companion_library
):
    fixture = companion_library
    for item in fixture["items"]:
        await refresh(database, fixture, item)
    async with database() as db, db.begin():
        source = await db.get(LibraryAsset, fixture["source_id"])
        source.state = "missing"
    for item in fixture["items"]:
        asset_id = await refresh(database, fixture, item)
        async with database() as db:
            asset = await db.get(LibraryAsset, asset_id)
            assert asset.version_id == fixture["ebook_id"] and asset.full_content
            assert asset.match_status == "matched" and asset.state == "present"
            assert all(file["import_verified"] for file in asset.files)
            assert (await db.get(AssetContains, (asset.id, fixture["work_id"]))).verified
            assert await db.scalar(select(func.count()).select_from(Version)) == 3


@pytest.mark.parametrize(
    "change", ["bytes", "missing", "extra", "route", "backend_disabled", "manual", "audio_metadata"]
)
async def test_changed_companion_is_held_without_reinterpreting_audio_metadata(
    database, companion_library, change
):
    fixture = companion_library
    item = fixture["items"][0].model_copy(deep=True)
    asset_id = await refresh(database, fixture, item)
    async with database() as db, db.begin():
        if change == "route":
            (await db.get(ImportDestination, fixture["route_id"])).backend_path = "/moved"
        elif change == "backend_disabled":
            library = await db.get(Library, fixture["library_id"])
            (await db.get(Integration, library.integration_id)).enabled = False
        elif change == "manual":
            other = Version(work_id=fixture["work_id"], medium="ebook", language="fr")
            db.add(other)
            await db.flush()
            link = await db.scalar(
                select(ProviderObject).where(
                    ProviderObject.external_id == item.id, ProviderObject.kind == "item:ebook"
                )
            )
            link.manual_lock, link.version_id = True, other.id
            manual_id = other.id
    if change == "bytes":
        fixture["copies"][0].write_bytes(b"different synthetic text")
    elif change == "missing":
        item.ebook, item.library_files = [], item.audio
    elif change == "extra":
        item.library_files.append(ABSFile(path=f"{item.path}/unknown.pdf", size=5, format="pdf"))
    elif change == "audio_metadata":
        item.narrators = ["A different recording"]
    await refresh(database, fixture, item)
    async with database() as db:
        asset = await db.get(LibraryAsset, asset_id)
        assert not asset.full_content and asset.match_status == "needs-review"
        assert not (await db.get(AssetContains, (asset.id, fixture["work_id"]))).verified
        source = await db.get(LibraryAsset, fixture["source_id"])
        assert source.full_content and source.state == "present"
        assert await db.scalar(select(func.count()).select_from(Version)) == (
            4 if change == "manual" else 3
        )
        if change == "manual":
            link = await db.scalar(
                select(ProviderObject).where(
                    ProviderObject.external_id == item.id, ProviderObject.kind == "item:ebook"
                )
            )
            assert link.manual_lock and link.version_id == manual_id


async def test_companion_retains_edition_across_merged_work_family(database, companion_library):
    fixture = companion_library
    item = fixture["items"][0]
    async with database() as db, db.begin():
        canonical = Work(title="Harbor", authors=["Alex Morgan"])
        db.add(canonical)
        await db.flush()
        (await db.get(Work, fixture["work_id"])).redirect_to = canonical.id
        audio = await db.scalar(
            select(LibraryAsset).where(
                LibraryAsset.external_id == item.id, LibraryAsset.medium == "audio"
            )
        )
        (await db.get(Version, audio.version_id)).work_id = canonical.id
        link = await db.scalar(
            select(ProviderObject).where(
                ProviderObject.external_id == item.id, ProviderObject.kind == "item:audio"
            )
        )
        link.work_id = canonical.id
        canonical_id = canonical.id
    asset_id = await refresh(database, fixture, item)
    async with database() as db:
        asset = await db.get(LibraryAsset, asset_id)
        assert asset.full_content and asset.version_id == fixture["ebook_id"]
        assert (await db.get(AssetContains, (asset_id, canonical_id))).verified
        assert await db.scalar(select(func.count()).select_from(Version)) == 3


@pytest.mark.parametrize("change", ["credentials", "disabled_route"])
async def test_publication_setting_changes_do_not_orphan_verified_copies(
    database, companion_library, change
):
    fixture = companion_library
    item = fixture["items"][0]
    asset_id = await refresh(database, fixture, item)
    async with database() as db, db.begin():
        library = await db.get(Library, fixture["library_id"])
        integration = await db.get(Integration, library.integration_id)
        if change == "credentials":
            integration.encrypted_secrets = encrypt_secrets({"token": "rotated-fixture"})
            integration.credential_generation += 1
        else:
            (await db.get(ImportDestination, fixture["route_id"])).enabled = False
        (await db.get(LibraryAsset, fixture["source_id"])).state = "missing"
    await refresh(database, fixture, item)
    async with database() as db:
        asset = await db.get(LibraryAsset, asset_id)
        assert asset.full_content and asset.version_id == fixture["ebook_id"]
        assert (await db.get(AssetContains, (asset_id, fixture["work_id"]))).verified


async def test_current_companion_identity_wins_over_retained_legacy_item(
    database, companion_library
):
    fixture = companion_library
    item = fixture["items"][0].model_copy(deep=True)
    async with database() as db, db.begin():
        library = await db.get(Library, fixture["library_id"])
        current_audio = await db.scalar(
            select(LibraryAsset).where(
                LibraryAsset.library_id == library.id,
                LibraryAsset.external_id == item.id,
                LibraryAsset.medium == "audio",
            )
        )
        current_copy = await db.scalar(
            select(EbookCompanion).where(EbookCompanion.target_asset_id == current_audio.id)
        )
        legacy_version = Version(work_id=fixture["work_id"], medium="ebook", language="fr")
        db.add(legacy_version)
        await db.flush()
        legacy_audio = LibraryAsset(
            library_id=library.id,
            external_id="legacy-audio",
            medium="audio",
            version_id=current_audio.version_id,
            full_content=True,
            match_status="matched",
            state="present",
        )
        legacy_ebook = LibraryAsset(
            library_id=library.id,
            external_id="legacy-audio",
            medium="ebook",
            version_id=legacy_version.id,
            full_content=True,
            match_status="matched",
            state="present",
        )
        legacy_link = ProviderObject(
            provider=f"abs:{library.integration_id}",
            kind="item:ebook",
            external_id="legacy-audio",
            work_id=fixture["work_id"],
            version_id=legacy_version.id,
            manual_lock=True,
            match_status="matched",
        )
        db.add_all([legacy_audio, legacy_ebook, legacy_link])
        await db.flush()
        db.add(
            EbookCompanion(
                library_id=library.id,
                source_asset_id=legacy_ebook.id,
                target_asset_id=legacy_audio.id,
                version_id=legacy_version.id,
                source_path="/books/legacy/Harbor.epub",
                target_path=current_copy.target_path,
                configuration=current_copy.configuration,
                receipt=current_copy.receipt,
                state="present",
            )
        )
        legacy_asset_id, legacy_link_id = legacy_ebook.id, legacy_link.id
        legacy_version_id = legacy_version.id
    current_id = await refresh(database, fixture, item)
    item.old_id = "legacy-audio"
    assert await refresh(database, fixture, item) == current_id
    async with database() as db:
        current = await db.get(LibraryAsset, current_id)
        assert current.full_content and current.version_id == fixture["ebook_id"]
        assert current.match_status == "matched"
        assert (await db.get(AssetContains, (current_id, fixture["work_id"]))).verified
        legacy = await db.get(LibraryAsset, legacy_asset_id)
        link = await db.get(ProviderObject, legacy_link_id)
        assert legacy.external_id == link.external_id == "legacy-audio"
        assert legacy.version_id == link.version_id == legacy_version_id
        assert legacy.full_content and link.manual_lock


async def test_companion_identity_follows_unambiguous_backend_rename(database, companion_library):
    fixture = companion_library
    item = fixture["items"][0].model_copy(deep=True)
    asset_id = await refresh(database, fixture, item)
    item.old_id, item.id = item.id, "renamed-audio"
    assert await refresh(database, fixture, item) == asset_id
    async with database() as db:
        asset = await db.get(LibraryAsset, asset_id)
        assert asset.full_content and asset.version_id == fixture["ebook_id"]
        assert asset.external_id == item.id and asset.match_status == "matched"
        assert (await db.get(AssetContains, (asset_id, fixture["work_id"]))).verified
