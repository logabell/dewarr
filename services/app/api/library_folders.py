"""Choose an ABS folder, persist its local mount, and activate a verified route."""

import asyncio
import logging
from pathlib import Path
from typing import Literal
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, field_validator, model_validator
from sqlalchemy import func, select

from app.adapters.audiobookshelf import Audiobookshelf, backend_path
from app.adapters.contracts import AdapterError
from app.adapters.grimmory import Grimmory
from app.api.automatic_imports import save_setup_preference
from app.api.automatic_imports import view as policy_view
from app.api.dependencies import Admin, Database
from app.config import ImportStorageRoute, get_settings
from app.db.models import (
    AcquisitionDefaults,
    AuditEvent,
    AutomaticImportPolicy,
    ImportDestination,
    ImportEntry,
    ImportStorageSettings,
    Integration,
    Library,
)
from app.domain.download_folders import browse_folders
from app.domain.operations import transaction_lock
from app.domain.release_profiles import DEFAULTS_LOCK
from app.importing.destination_view import DestinationView, view
from app.importing.destinations import check_library_route, choose_staging
from app.importing.filesystem import InspectionError
from app.importing.layout import check_staging_backend, overlaps, unsafe_staging
from app.importing.naming import StrictModel, fingerprint
from app.importing.planning import assert_admin
from app.importing.route_evidence import approval
from app.importing.seeding_rename import normalize_seeding_target
from app.importing.storage import storage_locations, storage_route, storage_settings
from app.security import decrypt_secrets

router = APIRouter(prefix="/organization/library-folders", tags=["organization"])
logger = logging.getLogger(__name__)
UNFINISHED_IMPORTS = (
    "queued",
    "publishing",
    "awaiting-library",
    "held",
    "cancelling",
    "cancel-held",
)


class FolderOption(StrictModel):
    library_id: UUID
    library_name: str
    server_name: str
    server_kind: str
    folders: list[str] = []
    ebooks_allowed: bool = True
    audio_allowed: bool = True
    error: str | None = None


def library_client(integration):
    secrets = decrypt_secrets(integration.encrypted_secrets)
    if integration.kind == "grimmory":
        return Grimmory(integration.base_url, secrets)
    return Audiobookshelf(integration.base_url, secrets["token"])


async def configuration(db, library_id):
    library = await db.get(Library, library_id)
    integration = await db.get(Integration, library.integration_id) if library else None
    if (
        not library
        or not library.accessible
        or not integration
        or not integration.enabled
        or integration.kind not in {"audiobookshelf", "grimmory"}
    ):
        raise HTTPException(422, "Choose a library from a connected library server")
    async with library_client(integration) as adapter:
        config = await adapter.import_configuration(library.external_id)
    return library, config


@router.get("", response_model=list[FolderOption])
async def folders(admin: Admin, db: Database):
    rows = (
        await db.execute(
            select(Library, Integration)
            .join(Integration, Library.integration_id == Integration.id)
            .where(
                Library.accessible.is_(True),
                Integration.enabled.is_(True),
                Integration.kind.in_(["audiobookshelf", "grimmory"]),
            )
            .order_by(Library.name)
        )
    ).all()

    async def option(library, integration):
        row = FolderOption(
            library_id=library.id,
            library_name=library.name,
            server_name=integration.name,
            server_kind=integration.kind,
        )
        try:
            async with library_client(integration) as adapter:
                config = await adapter.import_configuration(library.external_id)
            row.folders = config.folders
            row.ebooks_allowed = not config.audiobooks_only
            row.audio_allowed = getattr(config, "audio_allowed", True)
        except (AdapterError, InvalidToken, ValueError, KeyError) as error:
            logger.warning("Could not read folders for library %s", library.id, exc_info=True)
            row.error = "Could not read this library's folders. " + (
                str(error) if isinstance(error, AdapterError) else "Reconnect the library server."
            )
        return row

    return await asyncio.gather(*(option(library, integration) for library, integration in rows))


class LibraryFolderBrowseView(StrictModel):
    path: str | None
    parent: str | None
    directories: list[str]
    truncated: bool


@router.get("/browse", response_model=LibraryFolderBrowseView)
async def browse_library_folders(
    admin: Admin, db: Database, path: str | None = Query(default=None, max_length=2000)
):
    return await asyncio.to_thread(
        browse_folders, await storage_settings(db), path, include_libraries=True
    )


class FolderInput(StrictModel):
    library_id: UUID
    backend_path: str = Field(max_length=1024)
    local_path: str = Field(max_length=1024)
    destination_id: UUID | None = None
    expected_revision: str | None = None
    seeding_rename: bool = False
    client_path: str | None = Field(default=None, max_length=1024)
    automatic: bool | None = None

    @model_validator(mode="after")
    def seeding_target(self):
        self.seeding_rename, self.client_path = normalize_seeding_target(
            self.seeding_rename, self.client_path
        )
        return self

    @field_validator("backend_path")
    @classmethod
    def library_root(cls, value):
        return backend_path(value)

    @field_validator("local_path")
    @classmethod
    def worker_folder(cls, value):
        value = value.strip()
        path = Path(value)
        # A leading "//" is a UNC prefix. pathlib keeps it, then the directory
        # walk drops that prefix and opens a different local folder.
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or path.anchor != "/"
            or str(path) == "/"
            or ".." in path.parts
            or "\x00" in value
        ):
            raise ValueError("Choose an absolute folder path below /")
        return str(path)


@router.put("/{medium}", response_model=DestinationView)
async def choose(medium: Literal["ebook", "audio"], body: FolderInput, admin: Admin, db: Database):
    await transaction_lock(db, "library-storage")
    await assert_admin(db, admin.id)
    try:
        library, config = await configuration(db, body.library_id)
    except (AdapterError, InvalidToken, ValueError, KeyError) as error:
        raise HTTPException(
            422,
            "Could not read library folders. "
            + (
                str(error)
                if isinstance(error, AdapterError)
                else "Check the connection and credentials."
            ),
        ) from error
    if body.backend_path not in config.folders:
        raise HTTPException(422, "Choose a current folder from the selected library")
    if medium == "ebook" and config.audiobooks_only:
        raise HTTPException(
            422,
            "This library only accepts audiobooks. Choose another library or enable ebooks.",
        )
    if medium == "audio" and getattr(config, "audio_allowed", True) is False:
        raise HTTPException(422, "This library does not accept audiobooks. Choose another library.")
    root_key = f"library-{medium}"
    destination = (
        await db.get(ImportDestination, body.destination_id)
        if body.destination_id
        else await db.scalar(
            select(ImportDestination).where(ImportDestination.root_key == root_key)
        )
    )
    if body.destination_id and (not destination or destination.deleted_at):
        raise HTTPException(404, "Destination no longer exists")
    if destination:
        await transaction_lock(db, f"automatic-policy:{destination.id}")
        await db.refresh(destination, with_for_update=True)
        if (
            destination.medium != medium
            or body.expected_revision != (await view(db, destination)).revision
        ):
            raise HTTPException(
                409, "Folder settings changed. Reopen the folder picker and try again."
            )
        root_key = destination.root_key
    settings = await storage_settings(db)
    # Only the operator's setting fixes staging; a saved value is where an earlier pick put it.
    explicit = get_settings().import_staging_root
    local = Path(body.local_path)
    storage = await db.get(ImportStorageSettings, 1)
    previous = storage_route(settings, root_key)
    current = previous.staging_root if previous else None
    stage = await asyncio.to_thread(choose_staging, local, explicit, current)
    pending = 0
    if current:
        pending = await db.scalar(
            select(func.count())
            .select_from(ImportEntry)
            .where(
                ImportEntry.state.in_(UNFINISHED_IMPORTS),
                ImportEntry.specification["staging_root"].astext == str(current),
            )
        )
    if pending and (stage != current or local != settings.import_destinations.get(root_key)):
        raise HTTPException(
            409,
            f"Unfinished imports ({pending}) still use the staging folder {current}. "
            "Finish or cancel them before changing this destination's storage.",
        )
    journals = settings.import_journal_root
    # Legacy publications and combines keep their original journal/lock protocol.
    # Keep that protocol even when the ledger has no unfinished entries.
    for existing_stage, existing_journals in storage_locations(settings):
        if existing_stage == stage:
            journals = existing_journals
            break
    if previous and stage == current and previous.journal_root is not None:
        journals = previous.journal_root
    chosen_route = ImportStorageRoute(staging_root=stage, journal_root=journals)
    roots = {**settings.import_destinations, root_key: local}
    for root in roots.values():
        if unsafe_staging(root, stage) or any(
            overlaps(root, source) for source in settings.import_sources.values()
        ):
            raise HTTPException(
                422,
                "Library folders must be separate from downloads and staging, except for "
                "Dewarr's private .book-search-staging child folder.",
            )
    integration = await db.get(Integration, library.integration_id)
    try:
        check_staging_backend(
            integration.kind, local, stage, watcher_enabled=config.watcher_enabled
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if journals is not None and any(
        overlaps(journals, path)
        for path in [*roots.values(), *settings.import_sources.values(), stage]
    ):
        raise HTTPException(422, "Journal storage must be outside download and library mounts")
    try:
        await asyncio.to_thread(check_library_route, local, stage, [], journal_root=journals)
    except InspectionError as error:
        raise HTTPException(422, str(error)) from error
    if not storage:
        storage = ImportStorageSettings(id=1, destinations={}, sources={})
        db.add(storage)
    storage.destinations = {**storage.destinations, root_key: str(local)}
    routes = dict(storage.storage_routes or {})
    if previous and previous != chosen_route:
        retired = previous.model_dump(mode="json")
        routes["retired-" + fingerprint(retired)[:24]] = retired
    routes[root_key] = chosen_route.model_dump(mode="json")
    storage.storage_routes = routes
    if not destination:
        destination = ImportDestination(root_key=root_key)
        db.add(destination)
    destination.library_id, destination.medium = body.library_id, medium
    destination.backend_path, destination.mode, destination.enabled = (
        body.backend_path,
        "hardlink",
        True,
    )
    destination.seeding_rename, destination.client_path = body.seeding_rename, body.client_path
    destination.probe = destination.probe_token = destination.probe_operation_id = None
    await db.flush()
    if body.automatic is not None:
        await save_setup_preference(db, destination, admin, body.automatic)
    db.add(
        AuditEvent(
            actor_id=admin.id,
            action="organization.folder.selected",
            entity_id=destination.id,
            detail={"medium": medium},
        )
    )
    await db.commit()
    return await view(db, destination)


class ActivateInput(StrictModel):
    expected_revision: str
    automatic: bool = True


@router.post("/{destination_id}/activate", response_model=DestinationView)
async def activate(destination_id: UUID, body: ActivateInput, admin: Admin, db: Database):
    await transaction_lock(db, DEFAULTS_LOCK)
    await transaction_lock(db, f"automatic-policy:{destination_id}")
    await assert_admin(db, admin.id)
    destination = await db.get(ImportDestination, destination_id, with_for_update=True)
    if not destination or destination.deleted_at:
        raise HTTPException(404, "Destination not found")
    current = await view(db, destination)
    if current.revision != body.expected_revision or not current.publication_available:
        raise HTTPException(409, "The current folder must pass its route test before use")
    if body.automatic and not (await policy_view(db, destination)).can_enable:
        raise HTTPException(
            409, "Automatic import requires a verified folder and supported naming layout"
        )
    policy = await db.scalar(
        select(AutomaticImportPolicy)
        .where(AutomaticImportPolicy.destination_id == destination.id)
        .with_for_update()
    )
    if not policy:
        policy = AutomaticImportPolicy(
            destination_id=destination.id, generation=0, configuration={}, approved_by=admin.id
        )
        db.add(policy)
    policy.enabled, policy.approved_by = body.automatic, admin.id
    policy.generation += 1
    policy.configuration = {
        "destination_revision": current.revision,
        **approval(current.probe),
    }
    # One choice sets library identity and physical route together. Preserve unrelated preferences.
    for key, owner in [("installation", None), (f"user:{admin.id}", admin.id)]:
        defaults = await db.scalar(
            select(AcquisitionDefaults).where(AcquisitionDefaults.key == key)
        )
        if not defaults:
            defaults = AcquisitionDefaults(key=key, owner_id=owner, generation=0, preferences={})
            db.add(defaults)
        defaults.preferences = {
            **defaults.preferences,
            f"{destination.medium}_library_id": str(destination.library_id),
            f"{destination.medium}_destination_id": str(destination.id),
        }
        defaults.generation += 1
    db.add(
        AuditEvent(
            actor_id=admin.id,
            action="organization.folder.activated",
            entity_id=destination.id,
            detail={"medium": destination.medium, "automatic": body.automatic},
        )
    )
    await db.commit()
    return await view(db, destination)
