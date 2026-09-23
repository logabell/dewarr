"""Confirmed inventory satisfaction for an exact imported catalog version."""

from sqlalchemy import exists, or_, select

from app.db.models import AssetContains, Integration, Library, LibraryAsset
from app.domain.availability import owned_coverage


async def already_owned(db, version_id, library_id, *, inspection_id=None):
    from app.domain.download_recovery import replacement_exclusions

    excluded = await replacement_exclusions(db, inspection_id) if inspection_id else set()
    # One part of a recording shares its version with the others; it is not the book.
    parts = select(AssetContains.asset_id).where(
        AssetContains.asset_id == LibraryAsset.id, AssetContains.part_total.is_not(None)
    )
    complete = select(AssetContains.asset_id).where(
        AssetContains.asset_id == LibraryAsset.id, owned_coverage()
    )
    return await db.scalar(
        select(LibraryAsset.id)
        .join(Library)
        .join(Integration)
        .where(
            LibraryAsset.version_id == version_id,
            LibraryAsset.id.not_in(excluded),
            LibraryAsset.library_id == library_id,
            LibraryAsset.state == "present",
            LibraryAsset.full_content.is_(True),
            LibraryAsset.match_status.in_(["matched", "collection"]),
            or_(~exists(parts), exists(complete)),
            Library.accessible.is_(True),
            Integration.enabled.is_(True),
        )
        .limit(1)
    )
