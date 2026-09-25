"""Persistent UI-managed mounts, shared by API, worker and recovery tooling."""

import re
from pathlib import Path

from app.config import ImportStorageRoute, get_settings
from app.db.models import ImportStorageSettings

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
