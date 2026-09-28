"""Validate backend access and import settings for an explicitly mapped library.

Server release numbers are recorded on the route receipt. A newer Audiobookshelf
or Grimmory release stays usable; a route fails only when a required library
behavior is missing.
"""

from app.importing.layout import check_staging_backend, overlaps
from app.importing.publication import PublicationError


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
    if await adapter.import_configuration(library_id) != configuration:
        raise PublicationError("Grimmory library settings changed during verification")
    return {
        "version": version,
        "library_id": library_id,
        "configuration": configuration.model_dump(),
        "configuration_validated": True,
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
    if await adapter.import_configuration(library_id) != configuration:
        raise PublicationError("ABS library settings changed during verification")
    return {
        "version": version,
        "library_id": library_id,
        "configuration": configuration.model_dump(),
        "configuration_validated": True,
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
    # Read access is required for confirming the actual Bookdrop handoff.
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
    return {
        "version": capabilities.version,
        "configuration_validated": True,
        "workflow": "bookdrop",
        "scan_capable": False,
        "layout": "conventional",
    }
