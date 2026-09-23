"""Reviewed connection-generation changes; no torrent submission or route rewriting."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    AuditEvent,
    DownloadMembership,
    DownloadRepair,
    ImportDestination,
    Integration,
    Operation,
    SourceArtifact,
    SourceConnection,
    User,
)
from app.domain.acquisition_selection import configuration_current
from app.domain.downloaders import SETTINGS_LOCK, mapped_path
from app.domain.operations import transaction_lock
from app.domain.source_artifacts import member
from app.importing.destinations import destination_configuration
from app.importing.naming import fingerprint
from app.importing.storage import import_sources
from app.jobs.queue import enqueue


async def latest(db, attempt_id, state=None):
    query = select(DownloadRepair).where(DownloadRepair.attempt_id == attempt_id)
    if state:
        query = query.where(DownloadRepair.state == state)
    return await db.scalar(
        query.order_by(DownloadRepair.created_at.desc(), DownloadRepair.id).limit(1)
    )


async def accepted_configuration(db, selection):
    record = await db.scalar(
        select(DownloadRepair)
        .join(DownloadMembership, DownloadMembership.attempt_id == DownloadRepair.attempt_id)
        .where(DownloadMembership.selection_id == selection.id, DownloadRepair.state == "applied")
        .order_by(DownloadRepair.applied_at.desc(), DownloadRepair.id)
        .limit(1)
    )
    return record.configuration if record else None


async def require_repair_actor(db, repair):
    actor = await db.get(User, repair.actor_id, populate_existing=True)
    if repair.state != "pending" or not actor or not actor.active or actor.role != "admin":
        raise HTTPException(
            403, "Connection repair requires the reviewing administrator's current access"
        )


def destination_shape(value):
    # Credential generation can change, but the serving location/item contract cannot.
    backend = value.get("backend")
    return {
        **value,
        "backend": {k: v for k, v in backend.items() if k != "generation"} if backend else None,
    }


async def proposal(db, user, attempt, selection):
    await member(db, user.id)
    if user.role != "admin":
        raise HTTPException(403, "An administrator must review changed connections")
    if get_settings().recovery_mode:
        raise HTTPException(409, "Connection repair is paused for recovery")
    if (
        not attempt.external_may_exist
        or attempt.state in {"complete", "cancelled"}
        or selection.state != "committed"
    ):
        raise HTTPException(409, "Only an outstanding submitted transfer can use connection repair")
    if attempt.lease_until and attempt.lease_until > datetime.now(UTC):
        raise HTTPException(
            409, "A transfer check is running; review connections after it finishes"
        )
    if await latest(db, attempt.id, "pending"):
        raise HTTPException(409, "A reviewed connection repair is already pending")
    source_artifact = await db.get(SourceArtifact, selection.artifact_id)
    await transaction_lock(db, f"source:{source_artifact.source_key}")
    await transaction_lock(db, SETTINGS_LOCK)
    downloader = await db.get(Integration, selection.downloader_id, populate_existing=True)
    artifact = await db.get(SourceArtifact, selection.artifact_id)
    source = await db.get(SourceConnection, artifact.source_key, populate_existing=True)
    destination = await db.get(ImportDestination, selection.destination_id, with_for_update=True)
    if (
        not source
        or not source.enabled
        or not downloader
        or not downloader.enabled
        or downloader.status != "connected"
    ):
        raise HTTPException(409, "Enable and test the saved connections before reviewing a repair")
    frozen = selection.frozen
    if fingerprint({"url": downloader.base_url.rstrip("/")}) != attempt.endpoint_key:
        raise HTTPException(
            409, "The downloader endpoint changed; this repair cannot identify a different server"
        )
    if (
        downloader.config.get("save_path")
        != frozen.get("route_mapping", frozen["mapping"])["download_path"]
        or downloader.config.get("category") != frozen["downloader"]["category"]
        or mapped_path(downloader, downloader.config["save_path"], await import_sources(db))
        != frozen.get("route_mapping", frozen["mapping"])
    ):
        raise HTTPException(
            409,
            "Download paths or category changed; "
            "restore the submitted route before connection repair",
        )
    configuration = await destination_configuration(db, destination)
    if destination_shape(configuration) != destination_shape(frozen["destination"]):
        raise HTTPException(
            409, "The library destination changed; this repair cannot move an existing import"
        )
    desired = {
        "source_generation": source.generation,
        "downloader": {**frozen["downloader"], "generation": downloader.credential_generation},
        "destination": configuration,
    }
    if not await configuration_current(
        db,
        selection,
        committed=True,
        configuration=desired,
        version_identity_required=not attempt.external_may_exist,
    ):
        raise HTTPException(
            409,
            "Book identity or route verification changed; "
            "recheck the destination and selected version",
        )
    previous = await accepted_configuration(db, selection) or frozen
    changes = []
    if previous["source_generation"] != desired["source_generation"]:
        changes.append("Use the current source connection for this existing acquisition")
    if previous["downloader"] != desired["downloader"]:
        changes.append("Use the updated download client connection to observe the same transfer")
    if previous["destination"] != desired["destination"]:
        changes.append("Use the updated Audiobookshelf connection and reverified destination")
    if not changes:
        raise HTTPException(
            409, "No connection changes need review; check the existing transfer instead"
        )
    previous_record = await latest(db, attempt.id)
    revision = fingerprint(
        {
            "attempt_id": str(attempt.id),
            "configuration": desired,
            "selection": fingerprint(frozen),
            "previous_repair_id": str(previous_record.id) if previous_record else None,
        }
    )
    return desired, changes, revision


async def preview(db, user, identifier):
    from app.domain.download_attempts import locked, owned_attempt

    await owned_attempt(db, user, identifier)
    attempt, selection = await locked(db, identifier)
    _, changes, revision = await proposal(db, user, attempt, selection)
    return {"revision": revision, "changes": changes}


async def start(db, user, identifier, expected_revision, key):
    from app.domain.download_attempts import authority, locked, owned_attempt

    await transaction_lock(db, f"operation:{user.id}:{key}")
    await member(db, user.id)
    if user.role != "admin":
        raise HTTPException(403, "An administrator must review changed connections")
    await owned_attempt(db, user, identifier)
    receipt = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if receipt:
        if (
            receipt.kind != "acquisition.repair"
            or receipt.payload.get("attempt_id") != str(identifier)
            or receipt.payload.get("revision") != expected_revision
        ):
            raise HTTPException(409, "This command key was used for another repair")
        return await db.get(DownloadRepair, UUID(receipt.payload["repair_id"]))
    attempt, selection = await locked(db, identifier)
    desired, changes, revision = await proposal(db, user, attempt, selection)
    if revision != expected_revision:
        raise HTTPException(409, "Connection settings changed since the preview; review them again")
    await authority(db, selection, wanted=False, configuration=desired)
    operation = Operation(
        owner_id=user.id,
        kind="acquisition.repair",
        idempotency_key=key,
        message="Waiting to verify the existing transfer with the reviewed connections",
    )
    db.add(operation)
    await db.flush()
    repair = DownloadRepair(
        attempt_id=attempt.id,
        actor_id=user.id,
        operation_id=operation.id,
        command_key=key,
        revision=revision,
        configuration=desired,
        changes=changes,
        message=operation.message,
    )
    db.add(repair)
    await db.flush()
    operation.payload = {
        "attempt_id": str(attempt.id),
        "revision": revision,
        "repair_id": str(repair.id),
    }
    operation.job_id = await enqueue(db, "acquisition.download", attempt_id=str(attempt.id))
    (await db.get(Operation, attempt.operation_id)).job_id = operation.job_id
    attempt.state, attempt.message, attempt.next_check_at = (
        "uncertain",
        operation.message,
        datetime.now(UTC),
    )
    db.add(
        AuditEvent(
            actor_id=user.id,
            action="acquisition.repair.reviewed",
            entity_id=attempt.id,
            detail={"repair_id": str(repair.id)},
        )
    )
    return repair


async def finish(db, repair, state, message):
    repair.state, repair.message = state, message
    operation = await db.get(Operation, repair.operation_id)
    operation.status = "completed" if state == "applied" else "held"
    operation.message = message
    if state == "applied":
        repair.applied_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            actor_id=repair.actor_id,
            action="acquisition.repair." + state,
            entity_id=repair.attempt_id,
            detail={"repair_id": str(repair.id)},
        )
    )
