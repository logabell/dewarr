from sqlalchemy import exists, or_, select

from app.db.models import (
    AssetContains,
    BookList,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    ListEntry,
    User,
    Work,
)


def visible_owned_catalog(user, entity=Work):
    return or_(
        entity.catalog_owner_id == user.id,
        exists(
            select(ListEntry.id)
            .join(BookList)
            .where(
                ListEntry.work_id == entity.id,
                BookList.owner_id == entity.catalog_owner_id,
                BookList.shared.is_(True),
            )
        ),
    )


def visible_library(user: User):
    if user.role == "admin":
        return True
    return exists(
        select(LibraryGrant.library_id).where(
            LibraryGrant.library_id == Library.id,
            LibraryGrant.user_id == user.id,
        )
    )


def visible_origin_work(user: User, entity=Work):
    if user.role == "admin":
        return True
    return or_(
        entity.catalog_public.is_(True),
        visible_owned_catalog(user, entity),
        entity.id.in_(
            select(AssetContains.work_id)
            .join(LibraryAsset, AssetContains.asset_id == LibraryAsset.id)
            .join(Library, LibraryAsset.library_id == Library.id)
            .join(Integration, Library.integration_id == Integration.id)
            .where(
                Library.accessible.is_(True),
                Integration.enabled.is_(True),
                visible_library(user),
            )
        ),
    )


def visible_work(user: User):
    if user.role == "admin":
        return True
    from app.domain.work_graph import canonical_map

    # Resolve only origins carrying access evidence. Mapping every catalog work
    # makes every member-facing query pay for other users' private catalogs.
    owned_origins = select(Work.id).where(Work.catalog_owner_id == user.id).correlate(None)
    shared_origins = (
        select(ListEntry.work_id)
        .join(BookList, BookList.id == ListEntry.list_id)
        .join(Work, Work.id == ListEntry.work_id)
        .where(BookList.shared.is_(True), BookList.owner_id == Work.catalog_owner_id)
        .correlate(None)
    )
    library_origins = (
        select(AssetContains.work_id)
        .join(LibraryAsset, AssetContains.asset_id == LibraryAsset.id)
        .join(Library, LibraryAsset.library_id == Library.id)
        .join(Integration, Library.integration_id == Integration.id)
        .where(Library.accessible.is_(True), Integration.enabled.is_(True), visible_library(user))
    )
    mapping = canonical_map(owned_origins.union_all(shared_origins, library_origins))
    return or_(Work.catalog_public.is_(True), Work.id.in_(select(mapping.c.work_id)))
