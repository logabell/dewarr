"""Use the configured library folder when no destination override is needed."""

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models import ImportDestination, Integration, Library
from app.domain.visibility import visible_library


def configured_destinations(user):
    # Count unverified folders too: a failed probe must not select another library.
    return (
        select(ImportDestination)
        .join(Library)
        .join(Integration)
        .where(
            ImportDestination.enabled.is_(True),
            ImportDestination.deleted_at.is_(None),
            Library.accessible.is_(True),
            Integration.enabled.is_(True),
            Integration.deleted_at.is_(None),
            Integration.kind.in_(["audiobookshelf", "grimmory"]),
            visible_library(user),
        )
    )


async def destination_default(db, user, medium, library_id, saved_id):
    query = configured_destinations(user).where(ImportDestination.medium == medium)
    if saved_id:
        destination = await db.scalar(query.where(ImportDestination.id == saved_id))
        if not destination:
            raise HTTPException(
                409, "Saved library destination is unavailable. Check Settings → Libraries."
            )
        if not library_id or destination.library_id == library_id:
            return destination
        # A selected library scopes the destination choice. An installation's
        # default for another library must not force users to select a folder twice.
    if library_id:
        query = query.where(ImportDestination.library_id == library_id)
    destinations = list(await db.scalars(query))
    label = "audiobook" if medium == "audio" else "ebook"
    if not destinations:
        raise HTTPException(
            422,
            f"Set up an {label} library folder in Settings → Libraries before downloading.",
        )
    if len(destinations) > 1:
        raise HTTPException(
            422,
            f"Several {label} library folders are configured. "
            "Choose a default in Settings → Download preferences.",
        )
    return destinations[0]
