"""Persistent UI-managed mounts, shared by API, worker and recovery tooling."""

import re
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from app.config import ImportStorageRoute, get_settings
from app.db.models import ImportDestination, ImportStorageSettings

_KEY = re.compile(r"^[a-z0-9_-]{1,60}$")


def apply_storage(settings, destinations, staging_root, sources=None, routes=None):
    merged = dict(settings.import_sources)
    for key, path in (sources or {}).items():
        candidate = Path(path)
        if (
            isinstance(key, str)
            and _KEY.fullmatch(key)
            and key not in merged
            and candidate.is_absolute()
            and str(candidate) != "/"
            and ".." not in candidate.parts
        ):
            merged[key] = candidate
    return settings.model_copy(
        update={
            "import_sources": merged,
            "import_destinations": {
                **settings.import_destinations,
                **{key: Path(path) for key, path in destinations.items()},
            },
            "import_storage_routes": {
                **settings.import_storage_routes,
                **{
                    key: ImportStorageRoute.model_validate(route)
                    for key, route in (routes or {}).items()
                },
            },
            "import_staging_root": Path(staging_root)
            if staging_root
            else settings.import_staging_root,
        }
    )


async def storage_settings(db):
    row = await db.get(ImportStorageSettings, 1, populate_existing=True)
    return (
        apply_storage(
            get_settings(), row.destinations, row.staging_root, row.sources, row.storage_routes
        )
        if row
        else get_settings()
    )


async def import_sources(db):
    return (await storage_settings(db)).import_sources


async def shared_library_roots(db):
    """Physical paths configured for both formats, independent of library/folder names."""
    settings = await storage_settings(db)
    media = {}
    rows = await db.scalars(
        select(ImportDestination).where(
            ImportDestination.deleted_at.is_(None), ImportDestination.enabled.is_(True)
        )
    )
    for row in rows:
        root = settings.import_destinations.get(row.root_key)
        if root is not None:
            media.setdefault(root, set()).add(row.medium)
    return {root for root, formats in media.items() if {"ebook", "audio"} <= formats}


async def shared_naming_media(db, media, destinations, *, require_choice=True):
    """Resolve naming only for the destinations of this plan, never installation-wide."""
    settings = await storage_settings(db)
    shared = await shared_library_roots(db)
    rows = list(
        await db.scalars(
            select(ImportDestination).where(
                ImportDestination.deleted_at.is_(None), ImportDestination.enabled.is_(True)
            )
        )
    )
    result = set()
    for medium in media:
        candidates = [row for row in rows if row.medium == medium]
        if identifier := destinations.get(medium):
            candidates = [row for row in candidates if row.id == identifier]
            if not candidates:
                # A changed route is not evidence that the downloaded release is bad.
                raise HTTPException(409, f"Choose an enabled {medium} destination")
        modes = {settings.import_destinations.get(row.root_key) in shared for row in candidates}
        if len(modes) > 1:
            if require_choice:
                raise HTTPException(
                    422, f"Choose the {medium} destination before planning its folder names"
                )
        elif modes == {True}:
            result.add(medium)
    return result


def storage_route(settings, root_key):
    if root_key in settings.import_storage_routes:
        return settings.import_storage_routes[root_key]
    if root_key in settings.import_destinations and settings.import_staging_root:
        return ImportStorageRoute(staging_root=settings.import_staging_root)
    return None


def configured_storage_locations(roots):
    """Current and retained locations, including the environment-only legacy route."""
    pairs = {
        (
            Path(route["staging_root"]),
            Path(route["journal_root"]) if route.get("journal_root") else None,
        )
        for route in roots.get("import_storage_routes", {}).values()
    }
    if roots.get("import_staging_root"):
        pairs.add((Path(roots["import_staging_root"]), None))
    return pairs


def storage_locations(settings):
    return configured_storage_locations(settings.model_dump(mode="json"))


def frozen_storage_matches(settings, root_key, spec):
    route = storage_route(settings, root_key)
    return bool(
        route
        and route.staging_root == spec.staging_root
        and route.journal_root == spec.journal_root
    )
