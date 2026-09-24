from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select

from app.adapters.audiobookshelf import backend_path
from app.api.dependencies import Admin, Database
from app.api.imports import assert_admin
from app.api.operations import OperationView
from app.db.models import (
    AuditEvent,
    FrozenImportPlan,
    ImportDestination,
    Integration,
    Library,
    Operation,
)
from app.domain.downloaders import mapped_path, transfer_connection
from app.domain.operations import transaction_lock
from app.importing.destination_view import DestinationView, view
from app.importing.destinations import destination_configuration, permitted
from app.importing.naming import StrictModel
from app.importing.seeding_rename import normalize_seeding_target
from app.importing.storage import import_sources, storage_settings
from app.jobs.queue import enqueue

router = APIRouter(prefix="/organization", tags=["organization"])


class DestinationInput(StrictModel):
    library_id: UUID
    medium: Literal["ebook", "audio"]
    backend_path: str = Field(min_length=2, max_length=1024)
    mode: Literal["hardlink", "copy"] = "hardlink"
    seeding_rename: bool = False
    client_path: str | None = Field(default=None, max_length=1024)
    enabled: bool = True
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def backend_root(self):
        self.backend_path = backend_path(self.backend_path)
        self.seeding_rename, self.client_path = normalize_seeding_target(
            self.seeding_rename, self.client_path
        )
        return self


@router.get("/destination-roots", response_model=list[str])
async def destination_roots(admin: Admin, db: Database):
    return sorted((await storage_settings(db)).import_destinations)


@router.get("/destinations", response_model=list[DestinationView])
async def destinations(admin: Admin, db: Database):
    return [
        await view(db, row)
        for row in (
            await db.scalars(select(ImportDestination).order_by(ImportDestination.root_key))
        ).all()
    ]


@router.put("/destinations/{root_key}", response_model=DestinationView)
async def save_destination(root_key: str, body: DestinationInput, admin: Admin, db: Database):
    if root_key not in (await storage_settings(db)).import_destinations:
        raise HTTPException(422, "Select a library root configured on the worker")
    await transaction_lock(db, f"destination:{root_key}")
    await assert_admin(db, admin.id)
    library = await db.get(Library, body.library_id)
    integration = await db.get(Integration, library.integration_id) if library else None
    if not library or not library.accessible or not integration or not integration.enabled:
        raise HTTPException(422, "Select an accessible library from an enabled connection")
    row = await db.scalar(
        select(ImportDestination).where(ImportDestination.root_key == root_key).with_for_update()
    )
    if row and (await view(db, row)).revision != body.expected_revision:
        raise HTTPException(409, "Destination settings changed; reload before saving")
    if not row:
        if body.expected_revision:
            raise HTTPException(409, "Destination no longer matches the edited settings")
        row = ImportDestination(root_key=root_key)
        db.add(row)
    for key, value in body.model_dump(exclude={"expected_revision"}).items():
        setattr(row, key, value)
    row.probe, row.probe_token, row.probe_operation_id = None, None, None
    await db.flush()
    db.add(AuditEvent(actor_id=admin.id, action="organization.destination.saved", entity_id=row.id))
    await db.commit()
    return await view(db, row)


class ProbeInput(StrictModel):
    plan_id: UUID
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class SetupProbeInput(StrictModel):
    downloader_id: UUID
    downloader_generation: int = Field(ge=1)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.post(
    "/destinations/{destination_id}/setup-probe", response_model=OperationView, status_code=202
)
async def setup_probe(
    destination_id: UUID,
    body: SetupProbeInput,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    await transaction_lock(db, f"operation:{admin.id}:{idempotency_key}")
    command = {"destination_id": str(destination_id), **body.model_dump(mode="json")}
    existing = await db.scalar(
        select(Operation).where(
            Operation.owner_id == admin.id, Operation.idempotency_key == idempotency_key
        )
    )
    if existing:
        if (
            existing.kind != "organization.probe"
            or existing.payload.get("setup_command") != command
        ):
            raise HTTPException(409, "This operation key was already used for another command")
        return existing
    row = await db.scalar(
        select(ImportDestination).where(ImportDestination.id == destination_id).with_for_update()
    )
    if not row or not (await view(db, row)).configured:
        raise HTTPException(422, "Configure destination and private staging roots first")
    configuration = await destination_configuration(db, row)
    if (await view(db, row)).revision != body.expected_revision:
        raise HTTPException(409, "Destination settings changed; review them before probing")
    downloader = await transfer_connection(db, body.downloader_id)
    if not downloader.enabled or downloader.status != "connected":
        raise HTTPException(409, "Enable and test the downloader before checking its save folder")
    if downloader.credential_generation != body.downloader_generation:
        raise HTTPException(409, "Downloader settings changed; reload before probing")
    sources = await import_sources(db)
    mapping = mapped_path(downloader, downloader.config["save_path"], sources)
    operation = Operation(
        owner_id=admin.id,
        kind="organization.probe",
        idempotency_key=idempotency_key,
        message="Waiting to test the downloader save folder and library destination",
        payload={
            "destination_id": str(row.id),
            "configuration": configuration,
            "setup_command": command,
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
    operation.job_id = await enqueue(db, "organization.probe", operation_id=str(operation.id))
    await db.commit()
    await db.refresh(operation)
    return operation


@router.post("/destinations/{destination_id}/probe", response_model=OperationView, status_code=202)
async def probe_destination(
    destination_id: UUID,
    body: ProbeInput,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    await transaction_lock(db, f"operation:{admin.id}:{idempotency_key}")
    row = await db.scalar(
        select(ImportDestination).where(ImportDestination.id == destination_id).with_for_update()
    )
    if not row or not (await view(db, row)).configured:
        raise HTTPException(422, "Configure destination and private staging roots first")
    if (await view(db, row)).revision != body.expected_revision:
        raise HTTPException(409, "Destination settings changed; review them before probing")
    frozen = await db.scalar(
        select(FrozenImportPlan).where(
            FrozenImportPlan.id == body.plan_id, FrozenImportPlan.owner_id == admin.id
        )
    )
    if not frozen:
        raise HTTPException(404, "Import plan not found")
    document = frozen.document
    configuration = await destination_configuration(db, row)
    source = document["source"]
    if str((await import_sources(db)).get(source["key"])) != source["path"]:
        raise HTTPException(409, "Download root changed; inspect the files again")
    selected = next(
        (
            item
            for item in document["plan"]["items"]
            if item["state"] == "ready" and item["medium"] == row.medium
        ),
        None,
    )
    if not selected:
        raise HTTPException(
            422, "The plan needs a resolved item matching this destination's medium"
        )
    source_file = selected["files"][0]["source"]
    evidence = next(file for file in document["files"] if file["path"] == source_file)
    payload = {
        "destination_id": str(row.id),
        "plan_id": str(frozen.id),
        "configuration": configuration,
        "source_key": source["key"],
        "source_path": source["path"],
        "source_relative": source["relative_path"],
        **({"source_kind": "file"} if source.get("source_kind") == "file" else {}),
        "file": {
            "source": source_file,
            "name": "probe",
            "sha256": evidence["sha256"],
            "identity": evidence["identity"],
        },
    }
    existing = await db.scalar(
        select(Operation).where(
            Operation.owner_id == admin.id, Operation.idempotency_key == idempotency_key
        )
    )
    if existing:
        if existing.kind != "organization.probe" or existing.payload != payload:
            raise HTTPException(409, "This operation key was already used for another command")
        return existing
    operation = Operation(
        owner_id=admin.id,
        kind="organization.probe",
        idempotency_key=idempotency_key,
        payload=payload,
        message="Waiting to check the destination on the worker",
    )
    if not await permitted(db, operation, row):
        raise HTTPException(409, "Destination access or recovery mode prevents probing")
    db.add(operation)
    await db.flush()
    row.probe_operation_id, row.probe_token, row.probe = operation.id, None, None
    operation.job_id = await enqueue(db, "organization.probe", operation_id=str(operation.id))
    await db.commit()
    await db.refresh(operation)
    return operation
