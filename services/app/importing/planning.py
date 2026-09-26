"""Shared, caller-committed planning for reviewed and automatic imports."""

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID, uuid5

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.db.models import (
    AuditEvent,
    DownloadInspection,
    FrozenImportPlan,
    ProviderObject,
    User,
    Version,
    WorkMetadataSource,
)
from app.domain.catalog_titles import parse_title_labels
from app.domain.download_reviews import validate_inspection
from app.domain.operations import transaction_lock
from app.domain.work_graph import canonical_work, family_ids, graph_lock
from app.importing.collection_contents import ContainedWork
from app.importing.collection_contents import freeze as freeze_contents
from app.importing.grouping import current_grouping
from app.importing.inspection import InspectedFile
from app.importing.match_evidence import isbn_key
from app.importing.matching import GroupMatch, match_group
from app.importing.metadata import ExportMetadata, initial_sidecars
from app.importing.naming import (
    ImportGroup,
    ImportPlan,
    NamingMetadata,
    NamingProfile,
    PlannedSourceFile,
    StrictModel,
    fingerprint,
    plan_import,
)
from app.importing.settings import current_profile
from app.importing.storage import import_sources, shared_naming_media
from app.importing.versioning import version_revision
from app.importing.workflow import source_matches


async def filing_series(db, work):
    """The first standalone series of the book's accepted catalog record, if any."""
    sources = (
        await db.scalars(
            select(WorkMetadataSource)
            .where(
                WorkMetadataSource.work_id.in_(family_ids(work.id)),
                WorkMetadataSource.accepted.is_(True),
            )
            .order_by(WorkMetadataSource.provider != "hardcover", WorkMetadataSource.id)
        )
    ).all()
    for source in sources:
        for entry in (source.snapshot or {}).get("series") or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name.strip() and not entry.get("compilation"):
                position = entry.get("position")
                return name.strip()[:600], str(position)[:600] if position is not None else None
    return None, None


def release_part(group, row):
    """(N, M) when the selected files are one part of a book released in parts."""
    for value in (group.get("title"), PurePosixPath(row.relative_path or "").name):
        if value:
            labels = parse_title_labels(value)
            if labels.part and labels.part_total and labels.part_total >= 2:
                return labels.part, labels.part_total
    return None


class GroupSelection(StrictModel):
    group_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    work_id: UUID
    version_id: UUID
    full_content: bool
    match_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    contained_work_ids: list[UUID] = Field(default_factory=list, max_length=100)
    contents_confirmed: bool = False

    @model_validator(mode="after")
    def confirmed_contents(self):
        if self.contained_work_ids and (not self.contents_confirmed or not self.full_content):
            raise ValueError("Confirm the complete collection and every selected contained book")
        return self


class FreezeInput(StrictModel):
    inspection_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    grouping_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    selections: list[GroupSelection] = Field(min_length=1, max_length=100)
    include_covers: bool = True
    destinations: dict[Literal["ebook", "audio"], UUID] = Field(default_factory=dict)


class FrozenDocument(StrictModel):
    schema_version: int
    shared_media: list[Literal["ebook", "audio"]] = Field(default_factory=list)
    destinations: dict[Literal["ebook", "audio"], UUID] = Field(default_factory=dict)
    inspection_revision: str
    grouping_revision: str | None = None
    excluded_files: list[dict[str, str]] = Field(default_factory=list)
    profile: NamingProfile
    plan: ImportPlan
    groups: list[ImportGroup]
    source: dict[str, Any]
    files: list[InspectedFile]
    unselected_groups: list[str]
    publication_available: bool
    pending_checks: list[str]
    initial_sidecars: dict[str, dict[str, str]] = Field(default_factory=dict)
    version_revisions: dict[str, str] = Field(default_factory=dict)
    cover_sources: dict[str, str] = Field(default_factory=dict)
    matching_evidence: dict[str, GroupMatch] = Field(default_factory=dict)
    collection_contents: dict[str, list[ContainedWork]] = Field(default_factory=dict)


class FrozenPlanView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    inspection_id: UUID
    revision: str
    created_at: datetime
    document: FrozenDocument


async def assert_admin(db, user_id):
    actor = await db.get(User, user_id, populate_existing=True)
    if not actor or not actor.active or actor.role != "admin":
        raise HTTPException(403, "Administrator access is required")


async def owned_inspection(db, actor_id, inspection_id):
    row = await db.scalar(
        select(DownloadInspection).where(
            DownloadInspection.id == inspection_id, DownloadInspection.owner_id == actor_id
        )
    )
    if not row:
        raise HTTPException(404, "Inspection not found")
    await validate_inspection(db, row.id)
    return row


async def freeze_plan(db, admin, inspection_id: UUID, body: FreezeInput):
    await transaction_lock(db, f"inspection-plan:{inspection_id}")
    await assert_admin(db, admin.id)
    row = await owned_inspection(db, admin.id, inspection_id)
    from app.domain.recovery_approvals import require_current

    await require_current(db, "operation", row.operation_id)
    sources_match = source_matches(row, await import_sources(db))
    if row.state != "ready" or not row.snapshot or not sources_match:
        raise HTTPException(
            409, "A completed inspection of the configured download root is required"
        )
    if row.snapshot["revision"] != body.inspection_revision:
        raise HTTPException(409, "Inspection changed; review the current files")
    profile = await current_profile(db)
    if fingerprint(profile.model_dump()) != body.profile_revision:
        raise HTTPException(409, "Naming settings changed; preview the current profile")
    if len({selection.group_key for selection in body.selections}) != len(body.selections):
        raise HTTPException(422, "Choose each inspected group once")
    grouping_revision, grouping = await current_grouping(db, row)
    if (body.grouping_revision or row.snapshot["revision"]) != grouping_revision:
        raise HTTPException(
            409, "File groups changed; review their current membership before planning"
        )
    observed = {group.key: group.model_dump() for group in grouping.groups}
    files = {file["path"]: file for file in row.snapshot["files"]}
    groups, sidecars, versions, covers, matches, collections = [], {}, {}, {}, {}, {}
    await graph_lock(db)
    for selection in body.selections:
        group = observed.get(selection.group_key)
        if not group:
            raise HTTPException(422, "Selected group is not in this inspection")
        work = await canonical_work(db, selection.work_id)
        version = await db.get(Version, selection.version_id)
        if not version or (await canonical_work(db, version.work_id)).id != work.id:
            raise HTTPException(422, "Choose a catalog version belonging to the selected book")
        reviewed_group = next(item for item in grouping.groups if item.key == selection.group_key)
        await validate_inspection(db, row.id, version=version, group=reviewed_group)
        if version.medium != group["medium"]:
            raise HTTPException(422, "Catalog version and inspected medium differ")
        if selection.contained_work_ids:
            collections[str(uuid5(row.id, selection.group_key))] = await freeze_contents(
                db, selection.contained_work_ids, work.id
            )
        if selection.match_revision:
            match = await match_group(db, row.snapshot, grouping_revision, reviewed_group)
            if (
                match.revision != selection.match_revision
                or match.status != "matched"
                or match.selected_version_id != version.id
                or not any(
                    candidate.version_id == version.id and candidate.work_id == work.id
                    for candidate in match.candidates
                )
            ):
                raise HTTPException(
                    409, "Catalog matching evidence changed; refresh and review this group"
                )
            matches[str(uuid5(row.id, selection.group_key))] = match.model_dump(mode="json")
        pending = await db.scalar(
            select(ProviderObject.id)
            .where(
                ProviderObject.version_id == version.id,
                ProviderObject.match_status == "needs-review",
            )
            .limit(1)
        )
        if pending:
            raise HTTPException(
                409, "Resolve this version's metadata conflict before mapping files"
            )
        series, sequence = await filing_series(db, work)
        part = release_part(group, row)
        # Preserve version's origin work for correction/merge undo provenance.
        groups.append(
            ImportGroup(
                id=uuid5(row.id, selection.group_key),
                work_id=version.work_id,
                version_id=version.id,
                medium=version.medium,
                full_content=selection.full_content,
                metadata=NamingMetadata(
                    title=parse_title_labels(version.title).title
                    if part and version.title
                    else version.title or work.title,
                    authors=work.authors,
                    series=series,
                    sequence=sequence,
                    part_index=part[0] if part else None,
                    part_total=part[1] if part else None,
                    language=version.language or work.language,
                    narrators=version.narrators,
                    abridged=version.abridged,
                    original_year=work.publication_year,
                    edition_year=version.publication_year if version.medium == "ebook" else None,
                    recording_year=version.publication_year if version.medium == "audio" else None,
                    isbn=next(
                        (
                            value
                            for key in ("isbn13", "isbn_13", "isbn10", "isbn_10", "isbn")
                            if (value := isbn_key(version.identifiers.get(key)))
                        ),
                        None,
                    ),
                    asin=version.identifiers.get("asin")
                    if isinstance(version.identifiers.get("asin"), str)
                    else None,
                ),
                files=[PlannedSourceFile(**file) for file in group["files"]],
            )
        )
        try:
            if body.include_covers and work.cover_url:
                covers[str(groups[-1].id)] = work.cover_url
            versions[str(version.id)] = version_revision(version)
            sidecars[str(groups[-1].id)] = initial_sidecars(
                ExportMetadata(
                    medium=version.medium,
                    naming=groups[-1].metadata,
                    description=work.description,
                )
            )
        except ValueError as error:
            raise HTTPException(
                422,
                "Resolved book metadata cannot be exported; correct invalid or oversized fields",
            ) from error
    from app.domain.catalog_metadata import preferences

    shared_media = await shared_naming_media(
        db, {group.medium for group in groups}, body.destinations
    )
    plan = plan_import(
        groups,
        profile,
        combine_parts=(await preferences(db)).combine_library_parts,
        shared_media=shared_media,
    )
    # Replacements publish alongside the reported copy; no rename, overwrite or
    # deletion of library content is part of failed-download recovery.
    from app.db.models import AcquisitionSelection, DownloadAttempt, DownloadMembership
    from app.domain.download_recovery import replacement_folders

    replacements = list(
        await db.scalars(
            select(AcquisitionSelection)
            .join(DownloadMembership, DownloadMembership.selection_id == AcquisitionSelection.id)
            .join(DownloadAttempt, DownloadAttempt.id == DownloadMembership.attempt_id)
            .where(DownloadAttempt.inspection_id == row.id)
        )
    )
    replacement_folders(plan, replacements)
    selected_files = sorted({file.path for group in groups for file in group.files})
    document = {
        "schema_version": 2,
        "shared_media": sorted(shared_media),
        "destinations": {
            medium: str(identifier) for medium, identifier in body.destinations.items()
        },
        "initial_sidecars": sidecars,
        "version_revisions": versions,
        "cover_sources": covers,
        "matching_evidence": matches,
        **({"collection_contents": collections} if collections else {}),
        "inspection_revision": row.snapshot["revision"],
        "grouping_revision": grouping_revision,
        "excluded_files": [file.model_dump() for file in grouping.excluded],
        "profile": profile.model_dump(),
        "plan": plan.model_dump(mode="json"),
        "groups": [group.model_dump(mode="json") for group in groups],
        "source": {
            "key": row.source_key,
            "path": row.source_path,
            "relative_path": row.relative_path,
            **({"source_kind": "file"} if row.snapshot.get("source_kind") == "file" else {}),
            "directory_identity": row.snapshot["directory_identity"],
        },
        "files": [files[path] for path in selected_files],
        "unselected_groups": sorted(
            set(observed) - {selection.group_key for selection in body.selections}
        ),
        "publication_available": False,
        "pending_checks": [
            "source revalidation",
            "destination configuration",
            "hardlink/copy probe",
            "current ownership and permissions",
            "ABS layout certification",
        ],
    }
    revision = fingerprint(document)
    existing = await db.scalar(
        select(FrozenImportPlan).where(
            FrozenImportPlan.inspection_id == row.id, FrozenImportPlan.revision == revision
        )
    )
    if existing:
        return existing
    frozen = FrozenImportPlan(
        inspection_id=row.id, owner_id=admin.id, revision=revision, document=document
    )
    db.add(frozen)
    await db.flush()
    db.add(
        AuditEvent(
            actor_id=admin.id,
            action="organization.plan.frozen",
            entity_id=frozen.id,
            detail={"revision": revision},
        )
    )
    return frozen
