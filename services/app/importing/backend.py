"""Read backend import settings and prove the configured shared folder route.

Server release numbers are recorded on the route receipt. A newer Audiobookshelf
or Grimmory release stays usable; a route fails only when a required library
behavior is missing.
"""

import asyncio
import os
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.importing.filesystem import beneath, directory
from app.importing.layout import check_staging_backend, overlaps
from app.importing.publication import PublicationError, object_id, same_object, sync_directory

MAPPING_VISIBILITY_TIMEOUT = 5.0
MAPPING_POLL_INTERVAL = 0.1


async def path_visible(adapter, root, name, *, expected):
    """Observe a completed local change through bounded remote attribute caching.

    The absent/present/absent challenge is unchanged. Transport/permission errors
    still propagate; only a stale existence result is polled.
    """
    try:
        async with asyncio.timeout(MAPPING_VISIBILITY_TIMEOUT):
            # The remote filesystem offers no event/change notification here.
            while await adapter.path_exists(root, name) is not expected:  # noqa: ASYNC110
                await asyncio.sleep(MAPPING_POLL_INTERVAL)
            return True
    except TimeoutError:
        return False


@contextmanager
def mapping_marker(root: Path, name: str):
    if not re.fullmatch(r"book-search-check-[a-f0-9]{32}", name):
        raise PublicationError("Invalid mapping challenge name")
    with directory(root) as parent:
        os.mkdir(name, mode=0o755, dir_fd=parent)
        with beneath(parent, name, folder=True) as marker:
            sync_directory(parent)
            try:
                yield
                with directory(root) as current:
                    if not same_object(current, object_id(parent)):
                        raise PublicationError("Library root changed during mapping verification")
            finally:
                try:
                    with beneath(parent, name, folder=True) as current:
                        if not same_object(current, object_id(marker)):
                            raise PublicationError("Mapping marker changed; replacement preserved")
                    # Only an empty directory with our held identity can be removed.
                    os.rmdir(name, dir_fd=parent)
                    sync_directory(parent)
                except FileNotFoundError:
                    pass


async def verify_grimmory(
    adapter, library_id, backend_root, worker_root, medium, staging_root=None, mode="hardlink"
):
    from app.adapters.grimmory import AUDIO_EXTENSIONS

    version = await adapter.server_version()
    configuration = await adapter.import_configuration(library_id)
    if staging_root is not None:
        try:
            check_staging_backend(
                "grimmory", worker_root, staging_root, watcher_enabled=configuration.watcher_enabled
            )
        except ValueError as error:
            raise PublicationError(str(error)) from error
    capabilities, _ = await adapter.authorize()
    if backend_root not in configuration.folders:
        raise PublicationError("Selected path is not an exact folder root of this Grimmory library")
    if configuration.organization_mode != "BOOK_PER_FOLDER":
        raise PublicationError(
            "Choose a Grimmory library created with Book per folder organization, or use Bookdrop"
        )
    if medium == "ebook" and configuration.audiobooks_only:
        raise PublicationError("Choose a Grimmory library that accepts ebooks")
    if medium == "audio" and not configuration.audio_allowed:
        raise PublicationError("Choose a Grimmory library that accepts audiobooks")
    if "scan" not in capabilities.operations and not configuration.watcher_enabled:
        if staging_root is not None and overlaps(worker_root, staging_root):
            raise PublicationError(
                "Allow library scans in the Grimmory connection and keep its folder watcher "
                "disabled when staging inside this library"
            )
        raise PublicationError(
            "Enable Grimmory folder watch or provide a library-management connection"
        )
    if "metadata" not in capabilities.operations:
        raise PublicationError(
            "Grimmory needs permission to edit metadata "
            "so imported books keep their catalog details"
        )
    persistence = await adapter.metadata_persistence()
    check_grimmory_persistence(persistence, medium, mode)
    name = "book-search-check-" + uuid4().hex
    if await adapter.path_exists(backend_root, name):
        raise PublicationError("Unexpected existing mapping challenge; no directory was changed")
    with mapping_marker(worker_root, name):
        if not await path_visible(adapter, backend_root, name, expected=True):
            raise PublicationError("Worker and Grimmory do not see the same library folder")
    if not await path_visible(adapter, backend_root, name, expected=False):
        raise PublicationError("Grimmory still sees the removed challenge; mapping is not reliable")
    if await adapter.import_configuration(library_id) != configuration:
        raise PublicationError("Grimmory library settings changed during mapping verification")
    return {
        "version": version,
        "library_id": library_id,
        "configuration": configuration.model_dump(),
        "root_mapping": True,
        "scan_capable": "scan" in capabilities.operations,
        "watcher_enabled": configuration.watcher_enabled,
        "layout": "conventional",
        "audio_extensions": sorted(AUDIO_EXTENSIONS),
        "metadata_persistence": persistence,
    }


async def verify_backend(
    adapter,
    library_id,
    backend_root,
    worker_root,
    medium,
    *,
    staging_root=None,
    mode="hardlink",
    workflow="library",
):
    if workflow == "bookdrop":
        return await verify_bookdrop(adapter, backend_root, worker_root, staging_root)
    if getattr(adapter, "kind", "audiobookshelf") == "grimmory":
        return await verify_grimmory(
            adapter, library_id, backend_root, worker_root, medium, staging_root, mode
        )
    version = await adapter.server_version()
    configuration = await adapter.import_configuration(library_id)
    capabilities, _ = await adapter.authorize()
    if backend_root not in configuration.folders:
        raise PublicationError("Selected path is not an exact folder root of this ABS library")
    if medium == "ebook" and configuration.audiobooks_only:
        raise PublicationError("Disable Audiobooks only for this ebook destination in ABS")
    precedence = configuration.metadata_precedence
    known_sources = {
        "folderStructure",
        "audioMetatags",
        "nfoFile",
        "txtFiles",
        "opfFile",
        "absMetadata",
    }
    if (
        "opfFile" not in precedence
        or set(precedence) - known_sources
        or any(
            precedence.index(source) > precedence.index("opfFile")
            for source in ("folderStructure", "audioMetatags")
            if source in precedence
        )
    ):
        raise PublicationError(
            "ABS must apply OPF metadata after folder and embedded audio metadata"
        )
    if not precedence or precedence[-1] != "absMetadata":
        raise PublicationError(
            "Keep Audiobookshelf metadata (absMetadata) last in metadata "
            "precedence so edits made in ABS survive scans."
        )
    if "scan" not in capabilities.operations and not configuration.watcher_enabled:
        raise PublicationError("Enable the ABS watcher or provide a scan-capable connection")
    name = "book-search-check-" + uuid4().hex
    if await adapter.path_exists(backend_root, name):
        raise PublicationError("Unexpected existing mapping challenge; no directory was changed")
    with mapping_marker(worker_root, name):
        if not await path_visible(adapter, backend_root, name, expected=True):
            raise PublicationError("Worker and ABS do not see the same library folder")
    if not await path_visible(adapter, backend_root, name, expected=False):
        raise PublicationError("ABS still sees the removed challenge; mapping is not reliable")
    if await adapter.import_configuration(library_id) != configuration:
        raise PublicationError("ABS library settings changed during mapping verification")
    return {
        "version": version,
        "library_id": library_id,
        "configuration": configuration.model_dump(),
        "root_mapping": True,
        "scan_capable": "scan" in capabilities.operations,
        "watcher_enabled": configuration.watcher_enabled,
        "layout": "conventional",  # Nested watcher/import workflow matrix is not complete.
    }


def check_grimmory_persistence(persistence, medium, mode):
    if persistence["move_files"]:
        raise PublicationError(
            "Grimmory's Move files to library pattern is enabled globally. Use "
            "Bookdrop for Grimmory-managed naming, or disable that setting in "
            "Grimmory before using Dewarr's direct library import."
        )
    relevant = {"audiobook"} if medium == "audio" else {"epub", "pdf", "cbx"}
    if relevant.intersection(persistence["write_formats"]) and mode != "copy":
        raise PublicationError(
            "Grimmory writes embedded metadata for this format. Choose "
            "independent copies, or disable those writes in Grimmory, to "
            "protect downloaded and seeding files."
        )


async def verify_bookdrop(adapter, backend_root, worker_root, staging_root):
    if getattr(adapter, "kind", None) != "grimmory":
        raise PublicationError("Bookdrop requires Grimmory")
    if staging_root is None or overlaps(worker_root, staging_root):
        raise PublicationError(
            "Bookdrop staging must be outside its watched folder. Mount their "
            "common parent or configure external staging on the same "
            "filesystem."
        )
    capabilities, _ = await adapter.authorize()
    if "bookdrop" not in capabilities.operations:
        raise PublicationError("Grant this Grimmory account access to Bookdrop")
    # Read access is required for tracking handoff; the mapping challenge proves
    # the shared path, not whether Grimmory's watcher has ingested a particular file.
    await adapter.bookdrop_files()
    for library in await adapter.libraries():
        configuration = await adapter.import_configuration(library["id"])
        from pathlib import PurePosixPath

        candidate = PurePosixPath(backend_root)
        if any(
            candidate.is_relative_to(PurePosixPath(root))
            or PurePosixPath(root).is_relative_to(candidate)
            for root in configuration.folders
        ):
            raise PublicationError(
                "Bookdrop must be separate from every final Grimmory library folder"
            )
    name = "book-search-check-" + uuid4().hex
    if await adapter.path_exists(backend_root, name):
        raise PublicationError("Unexpected existing mapping challenge")
    with mapping_marker(worker_root, name):
        if not await path_visible(adapter, backend_root, name, expected=True):
            raise PublicationError("Dewarr and Grimmory do not see the same Bookdrop folder")
    if not await path_visible(adapter, backend_root, name, expected=False):
        raise PublicationError("Grimmory still sees the removed mapping challenge")
    return {
        "version": capabilities.version,
        "root_mapping": True,
        "workflow": "bookdrop",
        "scan_capable": False,
        "layout": "conventional",
    }
