"""Possible existing copies are advice, never automatic fulfillment evidence."""

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select

from app.db.models import AssetContains, LibraryAsset, Version
from app.domain.acquisition import asset_satisfies
from app.domain.availability import availability_rows
from app.domain.catalog_display import display_map
from app.domain.primary_editions import asset_narrators
from app.domain.work_graph import canonical_map, canonical_work


class ExistingCopyHint(BaseModel):
    asset_id: UUID
    work_id: UUID
    version_id: UUID | None
    title: str
    medium: str
    narrators: list[str]
    language: str | None
    state: str
    meets_requirements: bool


async def existing_copy_hints(db, user, work_id, spec):
    canonical = canonical_map()
    root = await canonical_work(db, work_id)
    display = display_map(user, [root.id])
    representative = (
        select(display.c.work_id).where(display.c.origin_id == root.id).scalar_subquery()
    )
    coverage = (
        select(func.count(func.distinct(canonical.c.work_id)))
        .select_from(AssetContains)
        .join(canonical, canonical.c.origin_id == AssetContains.work_id)
        .where(AssetContains.asset_id == LibraryAsset.id)
        .correlate(LibraryAsset)
        .scalar_subquery()
    )
    media = {medium for slot in spec.slots() for medium in spec.media(slot)}
    rows = await db.execute(
        availability_rows(user, display)
        .with_only_columns(LibraryAsset, Version, canonical.c.work_id, coverage)
        .join(canonical, canonical.c.origin_id == AssetContains.work_id)
        .outerjoin(Version, Version.id == LibraryAsset.version_id)
        .where(
            display.c.work_id == representative,
            canonical.c.work_id != root.id,
            LibraryAsset.medium.in_(media),
        )
        .order_by(LibraryAsset.created_at, LibraryAsset.id)
    )
    hints = {}
    for asset, version, origin, count in rows:
        hints[asset.id] = ExistingCopyHint(
            asset_id=asset.id,
            work_id=origin,
            version_id=asset.version_id,
            title=asset.title or (version.title if version else None) or root.title,
            medium=asset.medium,
            state=asset.state,
            narrators=asset_narrators(asset, version.narrators if version else None),
            language=version.language if version else None,
            meets_requirements=asset_satisfies(asset, version, count, spec.rule(asset.medium)),
        )
    return list(hints.values())
