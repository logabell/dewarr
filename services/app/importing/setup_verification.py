"""Shared setup checks for manual and automatic library verification."""

from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ImportDestination, Integration, Operation
from app.db.session import session_factory
from app.domain.downloaders import TRANSFER_KINDS, mapped_path, transfer_connection
from app.importing.destination_view import view
from app.importing.destinations import destination_configuration, permitted, probe_route
from app.importing.storage import import_sources
from app.jobs.queue import enqueue


async def prepare_probe(
    db,
    *,
    destination_id,
    downloader_id,
    downloader_generation,
    expected_revision,
    actor_id,
    idempotency_key,
):
    command = {
        "destination_id": str(destination_id),
        "downloader_id": str(downloader_id),
        "downloader_generation": downloader_generation,
        "expected_revision": expected_revision,
    }
    row = await db.scalar(
        select(ImportDestination).where(ImportDestination.id == destination_id).with_for_update()
    )
    if not row or row.deleted_at or not (await view(db, row)).configured:
        raise HTTPException(422, "Configure destination and private staging roots first")
    configuration = await destination_configuration(db, row)
    if (await view(db, row)).revision != expected_revision:
        raise HTTPException(409, "Destination settings changed; review them before probing")
    downloader = await transfer_connection(db, downloader_id)
    if not downloader.enabled or downloader.status != "connected":
        raise HTTPException(409, "Enable and test the downloader before checking its save folder")
    if downloader.credential_generation != downloader_generation:
        raise HTTPException(409, "Downloader settings changed; reload before probing")
    sources = await import_sources(db)
    mapping = mapped_path(downloader, downloader.config["save_path"], sources)
    operation = Operation(
        owner_id=actor_id,
        kind="organization.probe",
        idempotency_key=idempotency_key,
        message="Waiting to test the downloader save folder and library destination",
        payload={
            "destination_id": str(row.id),
            "configuration": configuration,
            "setup_command": command,
            "previous_probe": (await view(db, row)).probe or {},
            "setup_downloader": {
                "id": str(downloader.id),
                "generation": downloader.credential_generation,
                "mapping": mapping,
            },
            "source_key": mapping["source_key"],
            "source_path": str(sources[mapping["source_key"]]),
        },
    )
    if not await permitted(db, operation, row):
        raise HTTPException(409, "Destination access or recovery mode prevents probing")
    db.add(operation)
    await db.flush()
    row.probe_operation_id, row.probe_token, row.probe = operation.id, None, None
    return operation


async def verify_download_routes(user_id, *, job_id=None):
    if get_settings().recovery_mode:
        return
    async with session_factory()() as db:
        ids = list(
            await db.scalars(
                select(ImportDestination.id).where(
                    ImportDestination.deleted_at.is_(None), ImportDestination.enabled.is_(True)
                )
            )
        )
        clients = list(
            await db.scalars(
                select(Integration.id).where(
                    Integration.kind.in_(TRANSFER_KINDS),
                    Integration.owner_id.is_(None),
                    Integration.deleted_at.is_(None),
                    Integration.enabled.is_(True),
                    Integration.status == "connected",
                )
            )
        )
    for destination_id in ids:
        # A copy fallback invalidates earlier hardlink receipts once.
        for _ in range(2):
            initial_revision = None
            for client_id in clients:
                async with session_factory()() as db:
                    destination = await db.get(ImportDestination, destination_id)
                    if not destination or destination.deleted_at or not destination.enabled:
                        break
                    snapshot = await view(db, destination, include_routes=True)
                    initial_revision = initial_revision or snapshot.revision
                    client = await db.get(Integration, client_id)
                    if not client or (destination.seeding_rename and client.kind != "qbittorrent"):
                        continue
                    route = next(
                        (r for r in snapshot.client_routes if r.downloader_id == client_id), None
                    )
                    if (
                        not route
                        or route.status in {"verified", "unavailable"}
                        or not snapshot.configured
                    ):
                        continue
                    pending = (
                        await db.get(Operation, destination.probe_operation_id)
                        if destination.probe_operation_id
                        else None
                    )
                    if pending and pending.status in {"queued", "running"}:
                        if job_id is not None and pending.job_id == job_id:
                            # Resume the same durable operation after a worker restart.
                            operation_id = pending.id
                        else:
                            await enqueue(
                                db,
                                "organization.verify-download-routes",
                                user_id=str(user_id),
                                schedule_in=timedelta(seconds=10),
                            )
                            await db.commit()
                            return
                    else:
                        try:
                            operation = await prepare_probe(
                                db,
                                destination_id=destination_id,
                                downloader_id=client_id,
                                downloader_generation=client.credential_generation,
                                expected_revision=snapshot.revision,
                                actor_id=UUID(str(user_id)),
                                idempotency_key=f"automatic-route:{uuid4()}",
                            )
                        except HTTPException:
                            await db.rollback()
                            continue
                        operation.job_id = job_id
                        operation_id = operation.id
                        await db.commit()
                await probe_route(operation_id)
            async with session_factory()() as db:
                destination = await db.get(ImportDestination, destination_id)
                if not destination or (await view(db, destination)).revision == initial_revision:
                    break
