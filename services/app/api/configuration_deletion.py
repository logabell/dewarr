"""Remove saved configuration, keeping inert records for historical foreign keys."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import or_, select, update

from app.api.dependencies import Admin, Database
from app.api.downloaders import remember_sources, retired_keys
from app.api.library_folders import UNFINISHED_IMPORTS
from app.config import get_settings
from app.db.models import (
    AcquisitionDefaults,
    AcquisitionSelection,
    AuditEvent,
    AutomaticImportPolicy,
    DownloadAttempt,
    DownloadMembership,
    ImportDestination,
    ImportEntry,
    ImportStorageSettings,
    Integration,
    Library,
    PartCombine,
    SourceConnection,
)
from app.domain import downloaders, slskd_connection
from app.domain.operations import transaction_lock
from app.domain.release_profiles import DEFAULTS_LOCK
from app.domain.source_network import check_actor
from app.importing.destination_view import view as destination_view
from app.security import encrypt_secrets

router = APIRouter(tags=["configuration"])


def check_generation(current, expected):
    if current != expected:
        raise HTTPException(409, "Settings changed. Reload before deleting.")


async def clear_defaults(db, identifiers):
    for row in await db.scalars(select(AcquisitionDefaults).with_for_update()):
        preferences = {
            key: value
            for key, value in row.preferences.items()
            if not (key.endswith("_id") and str(value) in identifiers)
        }
        if preferences != row.preferences:
            row.preferences = preferences
            row.generation += 1


async def check_downloads(db, condition):
    active = await db.scalar(
        select(DownloadAttempt.id)
        .outerjoin(DownloadMembership, DownloadMembership.attempt_id == DownloadAttempt.id)
        .join(
            AcquisitionSelection,
            or_(
                DownloadAttempt.selection_id == AcquisitionSelection.id,
                DownloadMembership.selection_id == AcquisitionSelection.id,
            ),
        )
        .where(condition, DownloadAttempt.state.not_in(["complete", "cancelled"]))
        .limit(1)
    )
    if active:
        raise HTTPException(
            409,
            "Unfinished downloads use this configuration. "
            "Finish or cancel them in Downloads before deleting.",
        )
    if await db.scalar(
        select(AcquisitionSelection.id)
        .where(condition, AcquisitionSelection.state == "prepared")
        .limit(1)
    ):
        raise HTTPException(
            409, "A selected release uses this configuration. Cancel its selection before deleting."
        )


async def retire_destination(db, row):
    await transaction_lock(db, f"destination:{row.root_key}")
    await transaction_lock(db, f"automatic-policy:{row.id}")
    await db.refresh(row, with_for_update=True)
    await check_downloads(db, AcquisitionSelection.destination_id == row.id)
    pending = await db.scalar(
        select(ImportEntry.id)
        .where(ImportEntry.destination_id == row.id, ImportEntry.state.in_(UNFINISHED_IMPORTS))
        .limit(1)
    )
    combining = await db.scalar(
        select(PartCombine.id)
        .where(
            PartCombine.destination_id == row.id,
            PartCombine.state.in_(["combining", "separating", "needs-attention"]),
        )
        .limit(1)
    )
    if pending or combining:
        raise HTTPException(
            409,
            "Unfinished imports use this library folder. Finish or cancel them before deleting.",
        )
    await db.execute(
        update(AutomaticImportPolicy)
        .where(AutomaticImportPolicy.destination_id == row.id)
        .values(enabled=False, generation=AutomaticImportPolicy.generation + 1)
    )
    storage = await db.get(ImportStorageSettings, 1, with_for_update=True)
    if storage:
        storage.destinations = {
            key: value for key, value in storage.destinations.items() if key != row.root_key
        }
    await clear_defaults(db, {str(row.id)})
    row.enabled = False
    row.deleted_at = datetime.now(UTC)
    row.probe = row.probe_token = row.probe_operation_id = None
    # Free the unique root name for a fresh configuration without reusing its history.
    row.root_key = f"deleted-{row.id}"


async def retire_downloader(db, row):
    await db.refresh(row, with_for_update=True)
    await check_downloads(db, AcquisitionSelection.downloader_id == row.id)
    # Completed downloads can still be feeding an import; keep their connection until it finishes.
    destination_ids = select(AcquisitionSelection.destination_id).where(
        AcquisitionSelection.downloader_id == row.id,
        AcquisitionSelection.state == "committed",
    )
    if await db.scalar(
        select(ImportEntry.id)
        .where(
            ImportEntry.destination_id.in_(destination_ids),
            ImportEntry.state.in_(UNFINISHED_IMPORTS),
        )
        .limit(1)
    ):
        raise HTTPException(
            409, "Unfinished imports use this downloader. Finish or cancel them before deleting."
        )
    others = list(
        await db.scalars(
            select(Integration).where(
                Integration.kind.in_(downloaders.TRANSFER_KINDS),
                Integration.id != row.id,
                Integration.owner_id.is_(None),
                Integration.deleted_at.is_(None),
            )
        )
    )
    await remember_sources(
        db,
        {},
        retired_keys(row.config.get("mappings", []), [], get_settings().import_sources, others),
    )
    await clear_defaults(db, {str(row.id)})
    retire_integration(row)


def retire_integration(row):
    row.enabled = False
    row.deleted_at = datetime.now(UTC)
    row.credential_generation += 1
    row.encrypted_secrets = encrypt_secrets({})
    row.capabilities = {}
    row.status, row.last_error = "deleted", None
    row.lease_token = row.lease_until = row.next_sync_at = None


def retire_source(row):
    row.enabled = False
    row.deleted_at = datetime.now(UTC)
    row.generation += 1
    row.encrypted_secrets = encrypt_secrets({})
    row.base_url = ""
    row.proxy_url = None
    row.proxy_fallback_direct = True
    row.automation = {}
    row.status, row.last_error, row.last_success_at = "not-configured", None, None
    # Keep cooldowns and leases: deleting and reconnecting must not bypass a source's limits.


async def locks(db, admin):
    await transaction_lock(db, "library-storage")
    await transaction_lock(db, DEFAULTS_LOCK)
    await transaction_lock(db, downloaders.SETTINGS_LOCK)
    await check_actor(db, admin.id, admin=True)


@router.delete("/downloaders/{connection_id}", status_code=204)
async def delete_downloader(
    connection_id: UUID,
    admin: Admin,
    db: Database,
    expected_generation: int = Query(ge=0),
):
    await locks(db, admin)
    row = await downloaders.transfer_connection(db, connection_id)
    check_generation(row.credential_generation, expected_generation)
    if row.kind == "slskd":
        await transaction_lock(db, "source:slskd")
        source = await db.get(SourceConnection, "slskd", with_for_update=True)
        if source and not source.deleted_at:
            retire_source(source)
    await retire_downloader(db, row)
    db.add(AuditEvent(actor_id=admin.id, action="downloader.deleted", entity_id=row.id))
    await db.commit()


@router.delete("/integrations/{integration_id}", status_code=204)
async def delete_integration(integration_id: UUID, admin: Admin, db: Database):
    await locks(db, admin)
    row = await db.get(Integration, integration_id, with_for_update=True)
    if not row or row.deleted_at or row.owner_id or row.kind not in {"audiobookshelf", "grimmory"}:
        raise HTTPException(404, "Connection not found")
    library_ids = list(await db.scalars(select(Library.id).where(Library.integration_id == row.id)))
    for destination in await db.scalars(
        select(ImportDestination).where(
            ImportDestination.library_id.in_(library_ids), ImportDestination.deleted_at.is_(None)
        )
    ):
        await retire_destination(db, destination)
    await clear_defaults(db, {str(identifier) for identifier in library_ids})
    await db.execute(
        update(Library).where(Library.integration_id == row.id).values(accessible=False)
    )
    retire_integration(row)
    db.add(AuditEvent(actor_id=admin.id, action="integration.deleted", entity_id=row.id))
    await db.commit()


@router.delete("/organization/destinations/{destination_id}", status_code=204)
async def delete_destination(
    destination_id: UUID,
    admin: Admin,
    db: Database,
    expected_revision: str = Query(),
):
    await locks(db, admin)
    row = await db.get(ImportDestination, destination_id)
    if not row or row.deleted_at:
        raise HTTPException(404, "Library folder not found")
    await transaction_lock(db, f"destination:{row.root_key}")
    await db.refresh(row, with_for_update=True)
    if row.deleted_at:
        raise HTTPException(404, "Library folder not found")
    if (await destination_view(db, row)).revision != expected_revision:
        raise HTTPException(409, "Folder settings changed. Reload before deleting.")
    await retire_destination(db, row)
    db.add(
        AuditEvent(actor_id=admin.id, action="organization.destination.deleted", entity_id=row.id)
    )
    await db.commit()


@router.delete("/sources/{source}/connection", status_code=204)
async def delete_source(
    source: Literal["mam", "prowlarr", "audiobookbay", "slskd"],
    admin: Admin,
    db: Database,
    expected_generation: int = Query(ge=0),
):
    if source == "slskd":
        await locks(db, admin)
    else:
        await check_actor(db, admin.id, admin=True)
    await transaction_lock(db, f"source:{source}")
    row = await db.get(SourceConnection, source, with_for_update=True)
    if not row or row.deleted_at:
        raise HTTPException(404, "Source connection not found")
    check_generation(row.generation, expected_generation)
    if source == "slskd":
        downloader = await slskd_connection.integration(db)
        if downloader:
            await retire_downloader(db, downloader)
    retire_source(row)
    db.add(AuditEvent(actor_id=admin.id, action=f"source.{source}.deleted"))
    await db.commit()
