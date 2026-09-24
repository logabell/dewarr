import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.contracts import AdapterError
from app.adapters.grimmory import Grimmory
from app.config import get_settings
from app.db.models import AuditEvent, ImportDestination, Integration, Library, Operation, User
from app.db.session import session_factory
from app.domain.downloaders import TRANSFER_KINDS, mapped_path
from app.importing.backend import verify_backend
from app.importing.filesystem import InspectionError, describe_os_error, directory
from app.importing.naming import fingerprint
from app.importing.publication import PublishFile, probe_destination, probe_download_folder
from app.importing.storage import import_sources, storage_settings
from app.security import decrypt_secrets

logger = logging.getLogger(__name__)

STAGING_NAME = ".book-search-staging"


async def confirm_library_mapping(downloader_id, client_path, worker_root, *, client_factory=None):
    from app.importing.seeding_rename import confirm_library_mapping as confirm

    await confirm(downloader_id, client_path, worker_root, client_factory=client_factory)


async def destination_configuration(db, destination):
    library = await db.get(Library, destination.library_id, populate_existing=True)
    integration = (
        await db.get(Integration, library.integration_id, populate_existing=True)
        if library
        else None
    )
    settings = await storage_settings(db)
    root = settings.import_destinations.get(destination.root_key)
    return {
        "backend": {
            "integration_id": str(integration.id),
            "generation": integration.credential_generation,
            "base_url": integration.base_url,
            "enabled": integration.enabled,
            "library_external_id": library.external_id,
            "accessible": library.accessible,
            "kind": integration.kind,
        }
        if integration and library
        else None,
        "root_key": destination.root_key,
        "root_path": str(root) if root else None,
        "staging_path": str(settings.import_staging_root) if settings.import_staging_root else None,
        "library_id": str(destination.library_id),
        "medium": destination.medium,
        "backend_path": destination.backend_path,
        "mode": destination.mode,
        "seeding_rename": bool(destination.seeding_rename),
        "client_path": destination.client_path,
        "enabled": destination.enabled,
        # Independent valid library choices must not invalidate another medium's route.
        # Include any unsafe root so later mount changes still invalidate verification.
        "watched_paths": sorted(
            {
                str(path)
                for path in settings.import_destinations.values()
                if path == root
                or any(
                    path.is_relative_to(external) or external.is_relative_to(path)
                    for external in [
                        *settings.import_sources.values(),
                        *([settings.import_staging_root] if settings.import_staging_root else []),
                    ]
                )
            }
        ),
    }


async def permitted(db, operation, destination):
    actor = await db.get(User, operation.owner_id, populate_existing=True)
    library = await db.get(Library, destination.library_id, populate_existing=True)
    integration = (
        await db.get(Integration, library.integration_id, populate_existing=True)
        if library
        else None
    )
    return bool(
        actor
        and actor.active
        and actor.role == "admin"
        and library
        and library.accessible
        and integration
        and integration.kind in {"audiobookshelf", "grimmory"}
        and integration.enabled
        and destination.enabled
        and not get_settings().recovery_mode
    )


async def route_unchanged(db, destination, payload):
    return (
        await destination_configuration(db, destination) == payload["configuration"]
        and str((await import_sources(db)).get(payload["source_key"])) == payload["source_path"]
        and await setup_route_current(db, payload)
    )


async def setup_route_current(db, evidence):
    binding = evidence.get("setup_downloader")
    if not binding:
        return True
    row = await db.get(Integration, UUID(binding["id"]), populate_existing=True)
    if not row or row.kind not in TRANSFER_KINDS or row.owner_id is not None or not row.enabled:
        return False
    if row.credential_generation != binding["generation"] or row.status != "connected":
        return False
    try:
        mapping = mapped_path(row, row.config["save_path"], await import_sources(db))
    except (HTTPException, KeyError, ValueError):
        return False
    return mapping == binding["mapping"]


async def probe_route(operation_id: UUID, *, client_factory=None):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id)
        if not operation or operation.status in {"completed", "failed"}:
            return
        destination = await db.scalar(
            select(ImportDestination)
            .where(ImportDestination.id == UUID(operation.payload["destination_id"]))
            .with_for_update()
        )
        if not destination or destination.probe_operation_id != operation.id:
            operation.status, operation.message = (
                "failed",
                "A newer destination probe replaced this attempt",
            )
            return
        if not await permitted(db, operation, destination) or not await route_unchanged(
            db, destination, operation.payload
        ):
            operation.status, operation.message = (
                "failed",
                "Destination access or configuration changed",
            )
            destination.probe = None
            return
        destination.probe_token = token
        operation.status, operation.message = (
            "running",
            "Checking hardlink and no-replace behavior on worker mounts",
        )
        payload = operation.payload
        integration = await db.get(
            Integration, UUID(payload["configuration"]["backend"]["integration_id"])
        )
        try:
            secrets = decrypt_secrets(integration.encrypted_secrets)
            if integration.kind == "grimmory":
                secret = secrets
                if not secret.get("username") or not secret.get("password"):
                    raise ValueError("Missing username")
            else:
                secret = secrets["token"]
                if not isinstance(secret, str) or not secret:
                    raise ValueError("Missing token")
            factory = client_factory or (
                Grimmory if integration.kind == "grimmory" else Audiobookshelf
            )
        except (InvalidToken, KeyError, TypeError, ValueError):
            operation.status, operation.message = (
                "failed",
                "Library credentials could not be read; save the connection again",
            )
            destination.probe_token, destination.probe = None, None
            return
    copy_fallback = False
    seeding_rename = False
    try:
        configuration = payload["configuration"]
        seeding_rename = bool(configuration.get("seeding_rename"))
        for watched in configuration["watched_paths"]:
            watched = Path(watched)
            for external in (Path(payload["source_path"]), Path(configuration["staging_path"])):
                if external.is_relative_to(watched) or watched.is_relative_to(external):
                    raise InspectionError(
                        "Download and staging roots must be outside all library roots"
                    )
        await asyncio.to_thread(prepare_staging, Path(configuration["staging_path"]))
        if payload.get("setup_downloader"):
            report = await asyncio.to_thread(
                probe_download_folder,
                Path(payload["source_path"]),
                payload["setup_downloader"]["mapping"]["relative_path"],
                Path(configuration["root_path"]),
                Path(configuration["staging_path"]),
            )
        else:
            report = await asyncio.to_thread(
                probe_destination,
                Path(payload["source_path"]),
                payload["source_relative"],
                PublishFile.model_validate(payload["file"]),
                Path(configuration["root_path"]),
                Path(configuration["staging_path"]),
                **({"source_kind": "file"} if payload.get("source_kind") == "file" else {}),
            )
        copy_fallback = bool(
            not seeding_rename
            and configuration["mode"] == "hardlink"
            and report.get("no_replace")
            and report.get("copy")
            and not report.get("hardlink")
        )
        if seeding_rename:
            report["seeding_rename"] = True
            ok = bool(report.get("no_replace") and configuration.get("client_path"))
            if ok:
                binding = payload.get("setup_downloader")
                if not binding:
                    raise InspectionError(
                        "Check this folder from the library picker so Dewarr can "
                        "confirm qBittorrent sees it."
                    )
                await confirm_library_mapping(
                    binding["id"],
                    configuration["client_path"],
                    Path(configuration["root_path"]),
                )
        else:
            ok = (
                bool(report.get("no_replace") and report.get(configuration["mode"]))
                or copy_fallback
            )
        if ok:
            backend = configuration["backend"]
            async with factory(backend["base_url"], secret) as adapter:
                report["backend"] = await verify_backend(
                    adapter,
                    backend["library_external_id"],
                    configuration["backend_path"],
                    Path(configuration["root_path"]),
                    configuration["medium"],
                )

        uses_copy = (
            ok
            and not seeding_rename
            and not report.get("hardlink")
            and (copy_fallback or configuration["mode"] == "copy")
        )
        message = (
            "qBittorrent will rename completed downloads into this folder. "
            "The seeding file and the library file are the same copy."
            if ok and seeding_rename
            else "Hardlinks are unavailable for these folders. "
            "Downloads will be copied into the library."
            if uses_copy
            else "Filesystem and library folder mapping verified; ready for a reviewed import plan"
            if ok
            else "Could not prepare this folder for a seeding rename. "
            "Check the library path and try again."
            if seeding_rename
            else "Hardlink route unavailable; correct the mounts or explicitly choose copy mode"
        )
        if ok and report.get("warnings"):
            message = " ".join([message, *report["warnings"]])
    except (OSError, ValueError, AdapterError) as error:
        report, ok, copy_fallback = getattr(error, "probe_report", {}), False, False
        logger.warning("Library route probe %s failed", operation_id, exc_info=True)
        if isinstance(error, (InspectionError, AdapterError)):
            message = str(error)[:300]
        elif isinstance(error, OSError):
            message = (
                f"Download folder: {describe_os_error(error, Path(report['path']))} "
                "Review this client's folder mapping in Settings → Download clients."
                if report.get("folder_kind") == "download" and report.get("path")
                else f"Folder verification failed while {report['failure_step']} "
                f"({report['error_code']}). The folders were opened successfully, but this "
                "filesystem operation failed. Check the worker log for details."
                if report.get("failure_step")
                else describe_os_error(error)
            )
        else:
            message = "Destination probe failed; check paths, permissions and filesystem support"
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id)
        destination = await db.scalar(
            select(ImportDestination)
            .where(ImportDestination.id == UUID(payload["destination_id"]))
            .with_for_update()
        )
        if not destination or destination.probe_operation_id != operation.id:
            operation.status, operation.message = "failed", "A newer probe replaced this result"
            return
        if destination.probe_token != token:
            return
        if not await permitted(db, operation, destination) or not await route_unchanged(
            db, destination, payload
        ):
            operation.status, operation.message = (
                "failed",
                "Destination access changed; result discarded",
            )
            destination.probe_token, destination.probe = None, None
            return
        if ok and copy_fallback:
            destination.mode = "copy"
            configuration = await destination_configuration(db, destination)
        destination.probe = {
            "status": "verified" if ok else "failed",
            "message": message,
            "source_key": payload["source_key"],
            "source_path": payload["source_path"],
            "configuration_revision": fingerprint(configuration),
            "checked_at": datetime.now(UTC).isoformat(),
            **(
                {"setup_downloader": payload["setup_downloader"]}
                if payload.get("setup_downloader")
                else {}
            ),
            **report,
        }
        destination.probe_token = None
        operation.status, operation.message = "completed" if ok else "failed", message
        db.add(
            AuditEvent(
                actor_id=operation.owner_id,
                action="organization.destination.probed",
                entity_id=destination.id,
                detail={"verified": bool(ok)},
            )
        )


def _device(path: Path) -> int:
    with directory(path) as fd:
        return os.fstat(fd).st_dev


def choose_staging(local: Path, explicit: Path | None, current: Path | None) -> Path:
    """Keep a working staging folder on the library's filesystem, otherwise use the sibling."""
    if explicit:
        return explicit
    if current:
        try:
            if _device(current) == _device(local):
                return current
        except (OSError, InspectionError):
            pass
    return local.parent / STAGING_NAME


def holds_journals(staging: Path) -> bool:
    """Whether a staging folder still holds publication receipts that recovery reads."""
    try:
        with directory(staging) as fd, os.scandir(fd) as entries:
            return any(entry.name.endswith(".json") for entry in entries)
    except (OSError, InspectionError):
        return False


def check_library_route(local: Path, staging: Path, others: list[Path]) -> None:
    """Check a library folder choice against the real mounts before it is saved."""
    try:
        library = _device(local)
        if staging == local.parent / STAGING_NAME and _device(local.parent) != library:
            raise InspectionError(
                f"{local} is on a separate filesystem from {local.parent}, so Dewarr has "
                "nowhere beside it for its staging folder. Mount the parent folder instead "
                f"(for example /your/media:{local.parent}) and choose {local} again."
            )
    except OSError as error:
        raise InspectionError(describe_os_error(error, local)) from error
    try:
        prepare_staging(staging)
        staged = _device(staging)
    except OSError as error:
        raise InspectionError(describe_os_error(error, staging)) from error
    if staged != library:
        raise InspectionError(
            f"The staging folder {staging} is on a different filesystem from {local}. "
            "Mount a folder that contains both into Dewarr."
        )
    for other in others:
        try:
            device = _device(other)
        except (OSError, InspectionError):
            continue  # That library's own route test reports its missing folder.
        if device != library:
            raise InspectionError(
                f"{local} is on a different filesystem from the library folder {other}. "
                "Dewarr uses one staging folder for every library, so all library folders "
                "must be on the same mount."
            )


def prepare_staging(path):
    """Create only our private sibling staging directory; never follow symlinks."""
    if path.name != STAGING_NAME:
        return  # Existing explicitly configured staging keeps its previous contract.
    with directory(path.parent) as parent:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass  # private_staging subsequently verifies ownership, mode and no symlinks.
