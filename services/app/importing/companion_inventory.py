"""Reconcile explicitly placed ebook copies without inferring editions from audio tags."""

import asyncio
import time
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select, update

from app.adapters.audiobookshelf import IDENTITY_ISSUES
from app.db.models import (
    AssetContains,
    EbookCompanion,
    ImportDestination,
    LibraryAsset,
    ProviderObject,
    Version,
)
from app.domain.work_graph import canonical_work, family_ids
from app.importing.destinations import destination_configuration
from app.importing.naming import EBOOK
from app.importing.publication import PublicationError


def same_storage(current, frozen):
    """Credentials and publication policy do not change already placed bytes."""
    if not isinstance(frozen, dict):
        return False
    backend, old_backend = current.get("backend") or {}, frozen.get("backend") or {}
    return bool(
        backend.get("enabled")
        and backend.get("accessible")
        and all(
            current.get(key) == frozen.get(key)
            for key in ("library_id", "medium", "root_path", "backend_path")
        )
        and all(
            backend.get(key) == old_backend.get(key)
            for key in ("integration_id", "kind", "base_url", "library_external_id")
        )
    )


async def companion_rows(db, library, item, seen):
    query = (
        select(EbookCompanion)
        .join(LibraryAsset, LibraryAsset.id == EbookCompanion.target_asset_id)
        .where(
            EbookCompanion.library_id == library.id,
            LibraryAsset.library_id == library.id,
            LibraryAsset.medium == "audio",
        )
        .order_by(EbookCompanion.id)
        .limit(101)
    )
    rows = list(await db.scalars(query.where(LibraryAsset.external_id == item.id)))
    if rows or not item.old_id or item.old_id in seen:
        return rows
    # An alias cannot override a current item, even if that item has no copies.
    # Ordinary inventory migrates the old audio association only when the current
    # provider link does not exist; keep companion evidence on that same boundary.
    if await db.scalar(
        select(LibraryAsset.id).where(
            LibraryAsset.library_id == library.id,
            LibraryAsset.external_id == item.id,
            LibraryAsset.medium == "audio",
        )
    ) or await db.scalar(
        select(ProviderObject.id).where(
            ProviderObject.provider == f"abs:{library.integration_id}",
            ProviderObject.kind == "item:audio",
            ProviderObject.external_id == item.id,
        )
    ):
        return []
    return list(await db.scalars(query.where(LibraryAsset.external_id == item.old_id)))


async def apply_companion_item(db, library, item, generation, integration_id, now, seen, rows):
    """Return whether durable placement evidence owns this item's ebook observation.

    Known but unverifiable copies stay in review. They must never fall through to
    ordinary ebook matching, where the surrounding audio metadata describes a
    different edition. The canonical source may disappear without invalidating
    an independently verified copy.
    """
    if not rows:
        return False
    asset_query = select(LibraryAsset).where(
        LibraryAsset.library_id == library.id, LibraryAsset.medium == "ebook"
    )
    namespace = f"abs:{integration_id}"
    link_query = select(ProviderObject).where(
        ProviderObject.provider == namespace, ProviderObject.kind == "item:ebook"
    )
    asset = await db.scalar(asset_query.where(LibraryAsset.external_id == item.id))
    link = await db.scalar(link_query.where(ProviderObject.external_id == item.id))
    if not asset and not link and item.old_id and item.old_id not in seen:
        asset = await db.scalar(asset_query.where(LibraryAsset.external_id == item.old_id))
        link = await db.scalar(link_query.where(ProviderObject.external_id == item.old_id))
    observed = {
        file.path: file for file in [*item.library_files, *item.ebook] if file.format in EBOOK
    }
    if not asset and not observed:
        return True  # The backend has not seen the newly placed copy yet.
    if not link:
        link = ProviderObject(provider=namespace, kind="item:ebook", external_id=item.id)
        db.add(link)
    if not asset:
        asset = LibraryAsset(library_id=library.id, external_id=item.id, medium="ebook")
        db.add(asset)
        await db.flush()
    if asset.state == "intentionally-removed":
        asset.last_seen_at, asset.seen_generation = now, generation
        return True
    asset.external_id = link.external_id = item.id
    version_ids = {row.version_id for row in rows}
    version = await db.get(Version, rows[0].version_id)
    family = set(await db.scalars(family_ids(version.work_id))) if version else set()
    valid = bool(
        len(rows) <= 100
        and len(version_ids) == 1
        and version
        and version.medium == "ebook"
        and not item.missing
        and not item.invalid
        and not IDENTITY_ISSUES.intersection(item.read_issues)
        and not asset.containment
        and observed
        and set(observed) == {row.target_path for row in rows}
        and not (link.manual_lock and (link.version_id != version.id or link.work_id not in family))
    )
    if valid:
        from app.importing.colocate import verify_companion

        deadline = time.monotonic() + 30
        for row in rows:
            target = await db.get(LibraryAsset, row.target_asset_id)
            audio_version = await db.get(Version, target.version_id) if target.version_id else None
            try:
                destination = await db.get(
                    ImportDestination, UUID(row.configuration["target_destination_id"])
                )
            except (ValueError, KeyError, TypeError):
                valid = False
                break
            file = observed[row.target_path]
            valid = bool(
                row.state == "present"
                and audio_version
                and audio_version.work_id in family
                and not target.containment
                and target.full_content
                and target.match_status == "matched"
                and destination
                and not destination.deleted_at
                and destination.library_id == library.id
                and item.path
                and PurePosixPath(row.target_path).parent == PurePosixPath(item.path)
                and file.size > 0
                and file.size == row.receipt.get("size")
                and file.size_unit == "byte"
                and same_storage(
                    await destination_configuration(db, destination),
                    row.configuration.get("target"),
                )
            )
            if valid:
                try:
                    valid = bool(
                        await asyncio.to_thread(
                            verify_companion, row.configuration, row.receipt, deadline=deadline
                        )
                    )
                except (OSError, ValueError, KeyError, TypeError, PublicationError):
                    valid = False
            if not valid:
                break
    asset.title, asset.metadata_snapshot = item.title, item.model_dump(mode="json")
    asset.last_seen_at, asset.seen_generation = now, generation
    asset.state = "missing-suspected" if item.missing or item.invalid or not observed else "present"
    asset.missing_since = (
        (asset.missing_since or now) if asset.state == "missing-suspected" else None
    )
    asset.read_issues = list(item.read_issues) + ([] if valid else ["ebook_companion"])
    asset.full_content = valid
    asset.match_status = "matched" if valid else "needs-review"
    asset.files = [
        {**file.model_dump(), **({"import_verified": True} if valid else {})}
        for file in observed.values()
    ]
    if valid:
        work = await canonical_work(db, version.work_id)
        asset.version_id = version.id
        link.work_id, link.version_id, link.match_status = work.id, version.id, "matched"
        link.snapshot = item.model_dump(mode="json")
        await db.execute(
            update(AssetContains)
            .where(AssetContains.asset_id == asset.id, AssetContains.work_id != work.id)
            .values(verified=False)
        )
        coverage = await db.get(AssetContains, (asset.id, work.id))
        if not coverage:
            coverage = AssetContains(asset_id=asset.id, work_id=work.id)
            db.add(coverage)
        coverage.verified = True
        coverage.part_index = coverage.part_total = None
    else:
        link.match_status = "needs-review"
        await db.execute(
            update(AssetContains).where(AssetContains.asset_id == asset.id).values(verified=False)
        )
    return True
