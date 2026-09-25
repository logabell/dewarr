"""Library grants shared by the user and library settings editors."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.db.models import AuditEvent, Integration, Library, LibraryGrant, User
from app.domain.operations import transaction_lock


async def lock_access(db):
    # Both editors change the same join table from opposite directions. Serialize
    # those writes before reading the reviewed set, including currently empty sets.
    await transaction_lock(db, "settings:library-access")


async def user_libraries(db, user_id: UUID) -> list[UUID]:
    return list(
        await db.scalars(
            select(LibraryGrant.library_id)
            .where(LibraryGrant.user_id == user_id)
            .order_by(LibraryGrant.library_id)
        )
    )


async def replace_user_libraries(db, actor: User, user: User, desired, expected):
    if actor.role != "admin":
        raise HTTPException(403, "Only administrators can change library access")
    await lock_access(db)
    current = set(await user_libraries(db, user.id))
    if expected is None or set(expected) != current:
        raise HTTPException(409, "Library access changed. Reload the saved settings to review it.")
    desired = set(desired)
    if not user.active and desired - current:
        raise HTTPException(422, "Enable the account before adding library access")
    known = set(
        await db.scalars(
            select(Library.id)
            .join(Integration)
            .where(Library.id.in_(desired), Integration.deleted_at.is_(None))
        )
    )
    # Existing unavailable grants may be retained or removed, but not invented.
    if desired - known - current:
        raise HTTPException(422, "A selected library is unavailable. Reload the library choices.")
    if current != desired:
        await db.execute(delete(LibraryGrant).where(LibraryGrant.user_id == user.id))
        db.add_all([LibraryGrant(user_id=user.id, library_id=value) for value in desired])
        db.add(
            AuditEvent(
                actor_id=actor.id,
                action="user.library-access.changed",
                entity_id=user.id,
                detail={"library_ids": sorted(str(value) for value in desired)},
            )
        )
    return sorted(desired)
