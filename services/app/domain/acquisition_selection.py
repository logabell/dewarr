"""Reviewed, immutable source selection. Preparation performs no downloader calls."""

from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.adapters.nzb_descriptor import load_descriptor
from app.adapters.source_releases import release_value as parse_release
from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    AuditEvent,
    ImportDestination,
    Integration,
    Operation,
    ProviderObject,
    SourceArtifact,
    SourceConnection,
    Version,
)
from app.domain import narrators
from app.domain.acquisition import (
    RequestSpec,
    evaluate,
    language_accepts,
    release_unused,
    reserve,
    validate_request,
)
from app.domain.downloaders import (
    SETTINGS_LOCK,
    TORRENT_KINDS,
    USENET_KINDS,
    client_features,
    mapped_path,
    transfer_connection,
)
from app.domain.operations import transaction_lock
from app.domain.release_profiles import enforce_profile
from app.domain.request_constraints import constrained_preferences
from app.domain.request_preferences import for_selection
from app.domain.source_artifacts import artifact_bytes, member
from app.domain.work_graph import acquisition_lock, canonical_work
from app.importing.destinations import destination_configuration, setup_route_current
from app.importing.naming import fingerprint
from app.importing.route_evidence import receipts
from app.importing.storage import import_sources
from app.importing.versioning import version_revision


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_id: UUID
    slot: Literal["ebook", "audio", "either"]
    artifact_id: UUID
    downloader_id: UUID
    downloader_generation: int = Field(ge=1)
    destination_id: UUID
    destination_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_work_id: UUID
    search_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    profile_id: UUID | None = None
    profile_generation: int | None = Field(default=None, ge=0)
    profile_effective_revision: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )


async def owned_selection(db, user, identifier):
    row = await db.get(AcquisitionSelection, identifier, populate_existing=True)
    if not row or row.owner_id != user.id:
        raise HTTPException(404, "Release selection not found")
    return row


async def verified_probe(db, destination, configuration, mapping):
    for probe in receipts(destination.probe):
        binding = probe.get("setup_downloader")
        if (
            destination.enabled
            and await setup_route_current(db, probe)
            and (
                not binding
                or mapping.get("relative_path", binding["mapping"]["relative_path"])
                == binding["mapping"]["relative_path"]
            )
            and probe.get("status") == "verified"
            and probe.get("configuration_revision") == fingerprint(configuration)
            and probe.get("source_key") == mapping["source_key"]
            and probe.get("source_path")
            == str((await import_sources(db)).get(mapping["source_key"]))
            and probe.get("no_replace")
            and (
                probe.get("seeding_rename")
                if configuration.get("seeding_rename")
                else probe.get(destination.mode)
            )
            and probe.get("backend", {}).get("root_mapping")
        ):
            return True
    return False


def release_compatible(release, rule, version):
    if release.medium != rule["medium"]:
        raise HTTPException(422, "The source does not confirm the requested medium")
    required_language = rule["language"] or (version.language if version else None)
    if not language_accepts(required_language, release.language):
        raise HTTPException(422, "The source does not confirm the required language")
    if not narrators.accepts(rule.get("required_narrators", []), release.narrators):
        raise HTTPException(422, "The source does not confirm every required narrator")
    if version and version.medium == "audio" and version.narrators:
        if not narrators.accepts(version.narrators, release.narrators):
            raise HTTPException(
                422, "The source does not confirm the selected recording's narrators"
            )
    # Tracker labels remain claims. Exact version, completeness, abridgment and
    # standalone coverage must be verified against downloaded bytes before import.


async def reservation_for_release(db, intent, target, reservation, release):
    """Split a planned group when only another request rejects the chosen release.

    Call under the work acquisition lock. Shared reservations are an optimization,
    not additional requirements accepted by this reader. Never relax another
    request or change a reservation whose files have already been selected.
    """
    own_rule = RequestSpec.model_validate(intent.specification).rule(
        reservation.requirements["medium"]
    )
    if reservation.state != "planned" or own_rule == reservation.requirements:
        return reservation
    shared_version = (
        await db.get(Version, UUID(reservation.requirements["version_id"]))
        if reservation.requirements["version_id"]
        else None
    )
    try:
        release_compatible(release, reservation.requirements, shared_version)
    except HTTPException:
        own_version = (
            await db.get(Version, UUID(own_rule["version_id"])) if own_rule["version_id"] else None
        )
        try:
            release_compatible(release, own_rule, own_version)
        except HTTPException:
            return reservation
    else:
        return reservation
    separate = AcquisitionReservation(
        work_id=reservation.work_id,
        destination_id=reservation.destination_id,
        scope=reservation.scope,
        requirements=own_rule,
    )
    db.add(separate)
    await db.flush()
    target.reservation_id = separate.id
    await release_unused(db, intent.work_id)
    return separate


def version_evidence(version):
    return (
        {
            "id": str(version.id),
            "work_id": str(version.work_id),
            "medium": version.medium,
            "language": version.language,
            "abridged": version.abridged,
            "narrators": version.narrators,
        }
        if version
        else None
    )


async def prepare(db, user, body, key, *, automatic_evidence=None, recovery_selection_id=None):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Acquisition preparation is paused for recovery")
    command = body.model_dump(mode="json")
    if body.profile_id is None and body.profile_generation is None:
        command.pop("profile_id", None)
        command.pop("profile_generation", None)
    await transaction_lock(db, f"operation:{user.id}:{key}")
    await member(db, user.id)
    receipt = await db.scalar(
        select(Operation).where(
            Operation.owner_id == user.id,
            Operation.idempotency_key == key,
        )
    )
    if receipt:
        if receipt.kind != "acquisition.select" or receipt.payload.get("command") != command:
            raise HTTPException(409, "This selection key was already used for another command")
        return await owned_selection(db, user, UUID(receipt.payload["selection_id"]))
    intent = await db.get(AcquisitionIntent, body.intent_id)
    if not intent or intent.owner_id != user.id:
        raise HTTPException(404, "Request not found")
    from app.domain.permissions import require_download_allowed

    await require_download_allowed(db, user, intent)
    work = await acquisition_lock(db, intent.work_id)
    await member(db, user.id)
    if body.confirmed_work_id != work.id:
        raise HTTPException(409, "Confirm the current book record before selecting a release")
    await evaluate(db, user, intent)
    await db.flush()
    target = await db.scalar(
        select(AcquisitionTarget).where(
            AcquisitionTarget.intent_id == intent.id,
            AcquisitionTarget.slot == body.slot,
        )
    )
    if not target or target.state != "wanted" or not target.reservation_id:
        raise HTTPException(409, "This target is no longer wanted; refresh its library status")
    reservation = await db.get(AcquisitionReservation, target.reservation_id)
    if reservation.state == "selected":
        existing = await db.scalar(
            select(AcquisitionSelection).where(
                AcquisitionSelection.reservation_id == reservation.id,
                AcquisitionSelection.state == "prepared",
            )
        )
        if existing and existing.owner_id == user.id and existing.command == command:
            await selection_receipt(db, user, key, command, existing)
            return existing
        raise HTTPException(409, "A compatible request already has a selected release")
    if reservation.state != "planned":
        raise HTTPException(409, "This request needs reconciliation before selection")
    artifact = await db.get(SourceArtifact, body.artifact_id)
    if not artifact or artifact.owner_id != user.id:
        raise HTTPException(404, "Source artifact not found")
    await transaction_lock(db, f"source:{artifact.source_key}")
    source = await db.get(SourceConnection, artifact.source_key, populate_existing=True)
    if not source or not source.enabled or source.generation != artifact.source_generation:
        raise HTTPException(409, "The source connection changed; inspect the release again")
    descriptor = load_descriptor(artifact.descriptor)
    artifact_bytes(artifact)
    if descriptor.artifact_sha256 != artifact.sha256:
        raise HTTPException(409, "The saved release descriptor needs inspection again")
    release = parse_release(artifact.source_key, artifact.release_snapshot)
    profile = await for_selection(db, user, intent, body)
    if automatic_evidence:
        maximum = automatic_evidence["maximum_bytes"]
        profile = profile.model_copy(
            update={
                "preferences": profile.preferences.model_copy(
                    update={
                        "maximum_bytes": min(profile.preferences.maximum_bytes or maximum, maximum),
                    }
                )
            }
        )
    spec = RequestSpec.model_validate(intent.specification)
    if (
        body.slot == "either"
        and release.medium in {"audio", "ebook"}
        and release.medium != reservation.requirements["medium"]
    ):
        reservation = await reserve(db, user, intent, spec, "either", only_medium=release.medium)
        if reservation.state == "selected":
            raise HTTPException(409, "A compatible request already has a selected release")
        target.reservation_id = reservation.id
        await db.flush()
        await release_unused(db, intent.work_id)
    if not automatic_evidence and not recovery_selection_id:
        reservation = await reservation_for_release(db, intent, target, reservation, release)
    rule = reservation.requirements
    if recovery_selection_id:
        from app.domain.download_recovery import frozen_context

        recovery_root, rule, original_profile = await frozen_context(
            db, recovery_selection_id, rule, profile
        )
        if recovery_root.owner_id != user.id or recovery_root.target_id != target.id:
            raise HTTPException(409, "Replacement scope does not match the original request")
        enforce_profile(release, descriptor, original_profile)
    from app.domain import release_blocklist

    if await release_blocklist.blocked(db, work.id, rule["medium"], release, artifact.descriptor):
        raise HTTPException(409, "This release is blocklisted for this book and medium")
    profile = profile.model_copy(
        update={"preferences": constrained_preferences(profile.preferences, rule)}
    )
    enforce_profile(release, descriptor, profile)
    version = await db.get(Version, UUID(rule["version_id"])) if rule["version_id"] else None
    release_compatible(release, rule, version)
    await transaction_lock(db, SETTINGS_LOCK)
    route = await transfer_connection(db, body.downloader_id)
    if not route.enabled or route.credential_generation != body.downloader_generation:
        raise HTTPException(409, "Downloader settings changed; refresh before selecting")
    if route.status != "connected":
        raise HTTPException(409, "An administrator must test the saved downloader first")
    usenet = artifact.descriptor.get("protocol") == "nzb"
    if usenet != (release.protocol == "nzb"):
        raise HTTPException(409, "Saved release type does not match its inspected file")
    if artifact.source_key != "slskd" and usenet and route.kind not in USENET_KINDS:
        raise HTTPException(422, "Choose a SABnzbd or NZBGet connection for Usenet releases")
    if artifact.source_key != "slskd" and not usenet and route.kind not in TORRENT_KINDS:
        raise HTTPException(422, "Choose a torrent connection for torrent releases")
    if artifact.source_key == "slskd":
        from app.domain.slskd_connection import integration as soulseek_integration

        downloader = await soulseek_integration(db)
        if not downloader or not downloader.enabled or downloader.status != "connected":
            raise HTTPException(409, "Connect and test Soulseek before downloading this folder")
    else:
        downloader = route
    mapping = mapped_path(downloader, downloader.config["save_path"], await import_sources(db))
    destination = await db.scalar(
        select(ImportDestination)
        .where(
            ImportDestination.id == body.destination_id,
        )
        .with_for_update()
    )
    if not destination:
        raise HTTPException(404, "Import destination not found")
    configured_library = getattr(spec, rule["medium"] + "_library_id")
    if configured_library and destination.library_id != configured_library:
        raise HTTPException(422, "Use the library required by this request")
    await validate_request(
        db,
        user,
        intent.work_id,
        spec.model_copy(
            update={
                rule["medium"] + "_library_id": destination.library_id,
            }
        ),
    )
    configuration = await destination_configuration(db, destination)
    features = client_features(downloader)
    if not features["full_v2_hashes"] and descriptor.model_dump().get("infohash_v2"):
        raise HTTPException(
            422, "This client cannot verify full v2 torrent identities; choose qBittorrent"
        )
    if configuration.get("seeding_rename") and not features["in_client_rename"]:
        raise HTTPException(
            422,
            "Renaming the seeding copy is unavailable for this client; "
            "use a copy or hardlink destination",
        )
    if downloader.config.get("category") and not features["categories"]:
        raise HTTPException(422, "This client needs its Label plugin enabled for categories")
    if destination.medium != rule["medium"]:
        raise HTTPException(422, "Choose an import destination for the requested medium")
    if fingerprint(configuration) != body.destination_revision or not await verified_probe(
        db, destination, configuration, mapping
    ):
        raise HTTPException(409, "The download-to-library route needs a current verified probe")
    from app.domain.request_quotas import reserve_size

    await reserve_size(db, user, target, rule["medium"], descriptor.content_bytes)
    route_mapping = mapping
    save_path = downloader.config["save_path"]
    if downloader.kind == "deluge":
        save_path += "/dewarr-" + artifact.id.hex
        mapping = mapped_path(downloader, save_path, await import_sources(db))
    # This record is the frozen handoff for a future dispatch ledger. It cannot
    # be updated to silently change the source, target rules, route or client.
    selection = AcquisitionSelection(
        owner_id=user.id,
        intent_id=intent.id,
        target_id=target.id,
        reservation_id=reservation.id,
        artifact_id=artifact.id,
        downloader_id=downloader.id,
        destination_id=destination.id,
        command_key=key,
        command=command,
        frozen={
            "schema": 1,
            **(
                {"download_recovery": {"root_selection_id": str(recovery_selection_id)}}
                if recovery_selection_id
                else {}
            ),
            **({"automatic_selection": automatic_evidence} if automatic_evidence else {}),
            "work_id": str(work.id),
            "origin_work_id": str(intent.work_id),
            "work_title": work.title,
            "requirements": dict(rule),
            "version": version_evidence(version),
            **({"version_identity_revision": version_revision(version)} if version else {}),
            "slot": target.slot,
            "source_generation": artifact.source_generation,
            "artifact_sha256": artifact.sha256,
            "descriptor": descriptor.model_dump(mode="json"),
            "release": release.model_dump(mode="json"),
            "profile": profile.model_dump(mode="json"),
            **(
                {"recovery_profile": original_profile.model_dump(mode="json")}
                if recovery_selection_id
                else {}
            ),
            "downloader": {
                "id": str(downloader.id),
                "generation": downloader.credential_generation,
                "save_path": save_path,
                "category": downloader.config["category"],
            },
            "mapping": mapping,
            **({"route_mapping": route_mapping} if downloader.kind == "deluge" else {}),
            "destination": configuration,
            "verification": (
                "Automatically eligible candidate; actual content and versions require inspection"
                if automatic_evidence
                else "User-confirmed candidate; actual content and versions require inspection"
            ),
        },
    )
    db.add(selection)
    reservation.state = "selected"
    target.message = "Release selected; download has not started"
    await db.flush()
    await selection_receipt(db, user, key, command, selection)
    db.add(
        AuditEvent(actor_id=user.id, action="acquisition.release.selected", entity_id=selection.id)
    )
    return selection


async def selection_receipt(db, user, key, command, selection):
    db.add(
        Operation(
            owner_id=user.id,
            kind="acquisition.select",
            idempotency_key=key,
            status="completed",
            message="Release selection saved; download has not started",
            payload={"command": command, "selection_id": str(selection.id)},
        )
    )
    await db.flush()


async def cancel(db, user, selection):
    intent = await db.get(AcquisitionIntent, selection.intent_id)
    await acquisition_lock(db, intent.work_id)
    await member(db, user.id)
    await db.refresh(selection)
    if selection.state == "cancelled":
        return selection
    if selection.state != "prepared":
        raise HTTPException(409, "Use download activity to cancel an unsubmitted attempt")
    selection.state, selection.message = (
        "cancelled",
        "Release selection cancelled; no download was started",
    )
    reservation = await db.get(AcquisitionReservation, selection.reservation_id)
    reservation.state = "planned"
    await db.flush()
    await release_unused(db, intent.work_id)
    await evaluate(db, user, intent)
    db.add(
        AuditEvent(
            actor_id=user.id, action="acquisition.selection.cancelled", entity_id=selection.id
        )
    )
    return selection


async def configuration_current(
    db, selection, *, committed=False, configuration=None, version_identity_required=True
):
    """Validate routes and frozen identity before effects.

    Read-only transfer observation may skip version identity checks. Import
    authority independently fences the frozen revision before publication.
    """
    if (
        selection.state not in ({"prepared", "committed"} if committed else {"prepared"})
        or get_settings().recovery_mode
    ):
        return False
    frozen = {**selection.frozen, **(configuration or {})}
    artifact = await db.get(SourceArtifact, selection.artifact_id)
    source = await db.get(SourceConnection, artifact.source_key)
    downloader = await db.get(Integration, selection.downloader_id)
    destination = await db.get(ImportDestination, selection.destination_id)
    version_id = frozen["requirements"]["version_id"]
    version = (
        await db.get(Version, UUID(version_id), populate_existing=True) if version_id else None
    )
    if (
        version_identity_required
        and version
        and frozen.get("automatic_selection")
        and "version_identity_revision" not in frozen
    ):
        return False
    if (
        version_identity_required
        and version
        and frozen.get("automatic_selection")
        and await db.scalar(
            select(ProviderObject.id)
            .where(
                ProviderObject.version_id == version.id,
                ProviderObject.match_status == "needs-review",
            )
            .limit(1)
        )
    ):
        return False
    try:
        return bool(
            source
            and source.enabled
            and source.generation == frozen["source_generation"]
            and downloader
            and downloader.enabled
            and downloader.credential_generation == frozen["downloader"]["generation"]
            and destination
            and (not version_identity_required or version_evidence(version) == frozen["version"])
            and (
                not version_identity_required
                or "version_identity_revision" not in frozen
                or (version and version_revision(version) == frozen["version_identity_revision"])
            )
            and (await canonical_work(db, UUID(frozen["origin_work_id"]))).id
            == UUID(frozen["work_id"])
            and await destination_configuration(db, destination) == frozen["destination"]
            and mapped_path(downloader, downloader.config["save_path"], await import_sources(db))
            == frozen.get("route_mapping", frozen["mapping"])
            and mapped_path(downloader, frozen["downloader"]["save_path"], await import_sources(db))
            == frozen["mapping"]
            and await verified_probe(
                db,
                destination,
                frozen["destination"],
                frozen.get("route_mapping", frozen["mapping"]),
            )
        )
    except HTTPException:
        return False
