from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer, with_expression

from app.db.models import (
    AssetContains,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    User,
    Version,
)
from app.domain.catalog_display import display_map
from app.domain.primary_editions import asset_narrators, edition_order, primary_choices


class Availability(BaseModel):
    owned: bool = False
    ebook: bool = False
    audio: bool = False
    stale: bool = False
    in_collection: bool = False
    ebook_versions: int = 0
    audio_versions: int = 0
    ebook_stale: bool = False
    audio_stale: bool = False
    primary_ebook_version_id: UUID | None = None
    primary_audio_version_id: UUID | None = None
    primary_audio_narrators: list[str] = Field(default_factory=list)
    # Some parts of the book but not all: "2 of 3 parts". Not owned in that format.
    parts_owned: int = 0
    parts_total: int = 0
    parts_medium: str | None = None


async def availability_for(
    db: AsyncSession,
    user: User,
    work_ids: list[UUID],
    *,
    identity_only: bool = False,
) -> dict[UUID, Availability]:
    result = {work_id: Availability() for work_id in work_ids}
    if not work_ids:
        return result
    # Series membership must never inherit title-based presentation grouping.
    from app.domain.work_graph import canonical_map

    mapping = canonical_map() if identity_only else display_map(user, work_ids)
    roots = dict((await db.execute(select(mapping).where(mapping.c.origin_id.in_(work_ids)))).all())
    by_root = {}
    for origin, root in roots.items():
        by_root.setdefault(root, []).append(origin)
    query = (
        availability_rows(user, mapping)
        .with_only_columns(mapping.c.work_id, LibraryAsset, Version.narrators)
        .outerjoin(Version, Version.id == LibraryAsset.version_id)
        .options(
            defer(LibraryAsset.files),
            defer(LibraryAsset.read_issues),
            with_expression(
                LibraryAsset.metadata_snapshot,
                func.jsonb_build_object("narrators", LibraryAsset.metadata_snapshot["narrators"]),
            ),
        )
        .where(mapping.c.work_id.in_(by_root))
        .order_by(LibraryAsset.created_at, LibraryAsset.id)
    )
    choices = await primary_choices(db, user, mapping, list(by_root))
    copies = {root: {"ebook": [], "audio": []} for root in by_root}
    versions = {root: {"ebook": {}, "audio": {}} for root in by_root}
    for work_id, asset, version_narrators in (await db.execute(query)).all():
        narrators = asset_narrators(asset, version_narrators)
        if asset.medium in versions[work_id]:
            copies[work_id][asset.medium].append((asset, narrators))
            key = asset.version_id or asset.id
            prior = versions[work_id][asset.medium].get(key)
            if prior is None or (not prior and narrators):
                versions[work_id][asset.medium][key] = narrators
        for origin in by_root[work_id]:
            availability = result[origin]
            availability.owned = True
            availability.ebook |= asset.medium == "ebook"
            availability.audio |= asset.medium == "audio"
            availability.stale |= asset.state == "stale"
            availability.in_collection |= asset.containment is not None
    for root, formats in versions.items():
        primary = {}
        for medium, rows in copies[root].items():
            rows.sort(
                key=lambda row: edition_order(row[0], choices.get(root, {}).get(medium), row[1])
            )
            primary[medium] = rows[0] if rows else None
        for origin in by_root[root]:
            result[origin].ebook_versions = len(formats["ebook"])
            result[origin].audio_versions = len(formats["audio"])
            for medium in ("ebook", "audio"):
                rows = copies[root][medium]
                setattr(
                    result[origin],
                    medium + "_stale",
                    bool(rows) and all(a.state == "stale" for a, _ in rows),
                )
                setattr(
                    result[origin],
                    "primary_" + medium + "_version_id",
                    primary[medium][0].version_id if primary[medium] else None,
                )
            result[origin].primary_audio_narrators = primary["audio"][1] if primary["audio"] else []
    partial = (
        availability_rows(user, mapping, complete=False)
        .with_only_columns(
            mapping.c.work_id,
            LibraryAsset.medium,
            AssetContains.part_total,
            func.count(func.distinct(AssetContains.part_index)),
        )
        .where(mapping.c.work_id.in_(by_root), AssetContains.part_total.is_not(None))
        .group_by(
            mapping.c.work_id,
            LibraryAsset.medium,
            LibraryAsset.library_id,
            LibraryAsset.version_id,
            AssetContains.part_total,
        )
    )
    for root, medium, total, count in (await db.execute(partial)).all():
        for origin in by_root[root]:
            availability = result[origin]
            if count >= total or getattr(availability, medium):
                continue
            if count / total > availability.parts_owned / (availability.parts_total or 1):
                availability.parts_owned, availability.parts_total = count, total
                availability.parts_medium = medium
    return result


def owned_coverage(contains=AssetContains, asset=LibraryAsset):
    """A verified, complete copy of the book: the whole book, or every part of it.

    Parts count together only within one library and one version, so part 1 of one
    recording and part 2 of another never make a whole book.
    """
    sibling, copy = aliased(AssetContains), aliased(LibraryAsset)
    parts = (
        select(func.count(func.distinct(sibling.part_index)))
        .select_from(sibling)
        .join(copy, copy.id == sibling.asset_id)
        .where(
            sibling.work_id == contains.work_id,
            sibling.part_total == contains.part_total,
            sibling.verified.is_(True),
            copy.full_content.is_(True),
            copy.state.in_(["present", "stale"]),
            copy.library_id == asset.library_id,
            copy.version_id == asset.version_id,
        )
        .correlate(contains, asset)
        .scalar_subquery()
    )
    return and_(
        contains.verified.is_(True),
        asset.full_content.is_(True),
        or_(contains.part_total.is_(None), parts == contains.part_total),
    )


def availability_rows(user: User, mapping, *, complete=True):
    """Shared scoped ownership relation for projections and library-backed discovery.

    ``complete=False`` also returns parts of books whose other parts are missing.
    """
    query = (
        select(mapping.c.work_id, LibraryAsset.medium, LibraryAsset.state, LibraryAsset.containment)
        .select_from(AssetContains)
        .join(mapping, mapping.c.origin_id == AssetContains.work_id)
        .join(LibraryAsset, AssetContains.asset_id == LibraryAsset.id)
        .join(Library, LibraryAsset.library_id == Library.id)
        .join(Integration, Library.integration_id == Integration.id)
        .where(
            owned_coverage()
            if complete
            else and_(AssetContains.verified.is_(True), LibraryAsset.full_content.is_(True)),
            LibraryAsset.state.in_(["present", "stale"]),
            Library.accessible.is_(True),
            Integration.enabled.is_(True),
        )
    )
    if user.role != "admin":
        query = query.join(LibraryGrant, LibraryGrant.library_id == LibraryAsset.library_id).where(
            LibraryGrant.user_id == user.id
        )
    return query
