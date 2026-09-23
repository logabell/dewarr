"""Shared publication reservation command; caller commits plan/run/job together."""

from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    AuditEvent,
    DownloadInspection,
    FrozenImportPlan,
    ImportDestination,
    ImportEntry,
    ImportRun,
    Operation,
    Version,
)
from app.domain.download_reviews import validate_inspection
from app.domain.operations import transaction_lock
from app.importing.collection_contents import verify as verify_contents
from app.importing.converters import audio_conversion
from app.importing.destination_view import view as destination_view
from app.importing.destinations import destination_configuration
from app.importing.grouping import current_grouping
from app.importing.metadata import grimmory_sidecars
from app.importing.naming import StrictModel
from app.importing.ownership import already_owned
from app.importing.planning import assert_admin
from app.importing.publication import PublicationSpec, PublishFile
from app.importing.storage import import_sources
from app.importing.versioning import version_revision
from app.jobs.queue import enqueue

MERGE_PENDING_MESSAGE = "Waiting to merge MP3 chapters into one M4B"


class DestinationChoice(StrictModel):
    id: UUID
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ImportInput(StrictModel):
    plan_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    destinations: dict[Literal["ebook", "audio"], DestinationChoice]


async def start_import(db, admin, plan_id: UUID, body: ImportInput, idempotency_key: str):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Publication is paused for recovery")
    await transaction_lock(db, f"import-command:{admin.id}:{idempotency_key}")
    await assert_admin(db, admin.id)
    request = {"plan_id": str(plan_id), **body.model_dump(mode="json")}
    existing = await db.scalar(
        select(ImportRun).where(
            ImportRun.owner_id == admin.id, ImportRun.command_key == idempotency_key
        )
    )
    if existing:
        if existing.request != request:
            raise HTTPException(409, "This import command key was used for another request")
        return existing
    plan = await db.scalar(
        select(FrozenImportPlan).where(
            FrozenImportPlan.id == plan_id, FrozenImportPlan.owner_id == admin.id
        )
    )
    if not plan:
        raise HTTPException(404, "Import plan not found")
    from app.domain.recovery_approvals import require_current

    await require_current(db, "import-plan", plan.id)
    await transaction_lock(db, f"inspection-plan:{plan.inspection_id}")
    await validate_inspection(db, plan.inspection_id)
    inspection = await db.get(DownloadInspection, plan.inspection_id)
    grouping_revision, _ = await current_grouping(db, inspection)
    document = plan.document
    if (document.get("grouping_revision") or document["inspection_revision"]) != grouping_revision:
        raise HTTPException(409, "File groups changed after this plan; save a new reviewed plan")
    if plan.revision != body.plan_revision:
        raise HTTPException(409, "Review the current frozen import plan")
    if document["profile"]["layout"] != "conventional":
        raise HTTPException(409, "Nested publication awaits the complete compatibility gate")
    if not document.get("initial_sidecars") or not document.get("version_revisions"):
        raise HTTPException(409, "Create a fresh plan with frozen metadata and version evidence")
    source = document["source"]
    if str((await import_sources(db)).get(source["key"])) != source["path"]:
        raise HTTPException(409, "Download mapping changed; inspect and plan again")
    destinations = {}
    for medium, choice in body.destinations.items():
        row = await db.get(ImportDestination, choice.id)
        if not row or row.medium != medium or not row.enabled:
            raise HTTPException(422, "Choose an enabled destination for each medium")
        current = await destination_view(db, row)
        if (
            current.revision != choice.revision
            or not current.probe
            or current.probe.get("status") != "verified"
            or not current.probe.get("backend", {}).get("root_mapping")
        ):
            raise HTTPException(
                409, "Verify the current destination and ABS mapping before importing"
            )
        destinations[medium] = row
    # Short transaction serializes reservation decisions, not filesystem work.
    await transaction_lock(db, "import:reservations")
    run = ImportRun(
        owner_id=admin.id, plan_id=plan.id, command_key=idempotency_key, request=request
    )
    db.add(run)
    await db.flush()
    groups = {group["id"]: group for group in document["groups"]}
    files = {file["path"]: file for file in document["files"]}
    for item in document["plan"]["items"]:
        group = groups[item["group_id"]]
        destination = destinations.get(item["medium"])
        entry = ImportEntry(
            id=uuid4(),
            run_id=run.id,
            group_id=UUID(item["group_id"]),
            version_id=UUID(item["version_id"]),
            destination_id=destination.id if destination else None,
            state="held",
            reserved=False,
            message=item["reason"] or "Choose a destination for this book",
        )
        db.add(entry)
        if item["state"] != "ready" or not destination:
            continue
        version = await db.get(Version, entry.version_id)
        await validate_inspection(
            db, plan.inspection_id, destination_id=destination.id, version=version
        )
        if version_revision(version) != document["version_revisions"].get(str(version.id)):
            entry.message = "Catalog version changed; inspect its identity and create a fresh plan"
            continue
        contents = document.get("collection_contents", {}).get(item["group_id"], [])
        await verify_contents(db, contents)
        if await already_owned(
            db, version.id, destination.library_id, inspection_id=plan.inspection_id
        ):
            entry.state, entry.message = (
                "skipped",
                "This version is already confirmed in the destination library",
            )
            continue
        # A confirmed, specifically reported copy keeps its receipt and files,
        # but cannot reserve the version forever against its own replacement.
        from app.domain.download_recovery import replacement_exclusions

        excluded = await replacement_exclusions(db, plan.inspection_id)
        if excluded:
            for previous in await db.scalars(
                select(ImportEntry)
                .where(
                    ImportEntry.asset_id.in_(excluded),
                    ImportEntry.state == "confirmed",
                    ImportEntry.version_id == version.id,
                    ImportEntry.destination_id == destination.id,
                    ImportEntry.reserved.is_(True),
                )
                .with_for_update()
            ):
                previous.reserved = False
            await db.flush()
        reserved = await db.scalar(
            select(ImportEntry.id)
            .where(
                ImportEntry.configuration["destination"]["library_id"].astext
                == str(destination.library_id),
                ImportEntry.version_id == version.id,
                ImportEntry.reserved.is_(True),
            )
            .limit(1)
        )
        if reserved:
            entry.message = "Another import already reserves this version; review that import first"
            continue
        same_files = await db.scalar(
            select(ImportEntry.id)
            .join(ImportRun)
            .join(FrozenImportPlan)
            .where(
                FrozenImportPlan.inspection_id == plan.inspection_id,
                ImportEntry.group_id == entry.group_id,
                ImportEntry.reserved.is_(True),
            )
            .limit(1)
        )
        if same_files:
            entry.message = (
                "These files already belong to a reserved import; review that import first"
            )
            continue
        configuration = await destination_configuration(db, destination)
        try:
            conversion = audio_conversion(item, files, group["metadata"])
        except ValueError as error:
            entry.message = str(error)[:500]
            continue
        converted = {chapter.source for chapter in conversion.chapters} if conversion else set()
        sidecars = dict(document["initial_sidecars"][item["group_id"]])
        published_names = [
            PurePosixPath(mapping["destination"]).name
            for mapping in item["files"]
            if mapping["source"] not in converted
        ]
        if conversion:
            published_names.append(conversion.output_name)
        if (configuration["backend"] or {}).get("kind") == "grimmory":
            sidecars.update(grimmory_sidecars(group["metadata"], item["medium"], published_names))
        # A merged audiobook is a new file, so the torrent stays in the download folder.
        rename_seeding = bool(configuration.get("seeding_rename")) and conversion is None
        if rename_seeding and not configuration.get("client_path"):
            entry.message = (
                "Enter the library folder qBittorrent uses before renaming the seeding copy"
            )
            continue
        specification = PublicationSpec(
            entry_id=entry.id,
            plan_revision=plan.revision,
            source_root=Path(source["path"]),
            source_relative=source["relative_path"],
            source_kind=source.get("source_kind", "directory"),
            source_directory=source["directory_identity"],
            destination_root=Path(configuration["root_path"]),
            staging_root=Path(configuration["staging_path"]),
            folder=item["folder"].split("/", 1)[1],
            mode="rename" if rename_seeding else destination.mode,
            files=[
                PublishFile(
                    source=mapping["source"],
                    name=PurePosixPath(mapping["destination"]).name,
                    sha256=files[mapping["source"]]["sha256"],
                    identity=files[mapping["source"]]["identity"],
                )
                for mapping in item["files"]
                if mapping["source"] not in converted
            ],
            conversion=conversion,
            sidecars=sidecars,
        )
        entry.specification = specification.model_dump(mode="json")
        entry.configuration = {
            "destination": configuration,
            "source_key": source["key"],
            "source_path": source["path"],
        }
        entry.expected_metadata = {
            **group["metadata"],
            "medium": item["medium"],
            "version_revision": version_revision(version),
            "cover_source": document.get("cover_sources", {}).get(item["group_id"]),
            **({"collection_contents": contents} if contents else {}),
        }
        if item["medium"] == "ebook":
            main = {file["path"] for file in group["files"] if file.get("role", "media") == "media"}
            entry.expected_metadata["ebook_media_paths"] = [
                str(PurePosixPath(configuration["backend_path"]) / specification.folder / file.name)
                for file in specification.files
                if file.source in main
            ]
        if (
            item["medium"] == "audio"
            and not specification.conversion
            and sum(file.get("role", "media") == "media" for file in group["files"]) > 1
        ):
            names = {file.source: file.name for file in specification.files}
            entry.expected_metadata["audio_order"] = [
                str(
                    PurePosixPath(configuration["backend_path"])
                    / specification.folder
                    / names[file["path"]]
                )
                for file in sorted(
                    group["files"],
                    key=lambda file: (file.get("disc") or 1, file.get("track") or 1, file["path"]),
                )
                if file.get("role", "media") == "media"
            ]
        entry.state, entry.message, entry.reserved = (
            "queued",
            MERGE_PENDING_MESSAGE if specification.conversion else "Waiting to publish this book",
            True,
        )
        operation = Operation(
            owner_id=admin.id,
            kind="organization.publish",
            idempotency_key=f"import:{entry.id}",
            payload={"entry_id": str(entry.id)},
        )
        db.add(operation)
        await db.flush()
        entry.operation_id = operation.id
        operation.job_id = await enqueue(db, "organization.publish", operation_id=str(operation.id))
    db.add(AuditEvent(actor_id=admin.id, action="organization.import.requested", entity_id=run.id))
    await db.flush()
    return run
