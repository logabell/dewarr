"""Review and prepare finite extra-book selections on a manually chosen artifact."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.source_releases import release_value as parse_release
from app.adapters.torrent_descriptor import TorrentDescriptor
from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    ImportDestination,
    Operation,
    SourceArtifact,
)
from app.domain import acquisition, acquisition_selection, list_series, pack_coverage
from app.domain.acquisition import RequestReason, RequestSpec
from app.domain.acquisition_selection import SelectionInput
from app.domain.download_memberships import require_same_transfer
from app.domain.list_requests import pending_targets
from app.domain.operations import transaction_lock
from app.domain.release_profiles import ProfileSnapshot, refresh_profile, same_profile
from app.domain.request_constraints import constrained_preferences
from app.domain.source_artifacts import artifact_bytes
from app.domain.work_graph import acquisition_lock, canonical_work, graph_lock
from app.importing.naming import fingerprint

KIND = "acquisition.pack-prepare"


def child_specification(intent, selection, destination):
    values = RequestSpec.model_validate(intent.specification).model_dump(mode="json")
    medium = selection.frozen["requirements"]["medium"]
    values.update(mode=medium, preferred_medium=None, ebook_version_id=None, audio_version_id=None)
    values[medium + "_library_id"] = str(destination.library_id) if destination.library_id else None
    values[("audio" if medium == "ebook" else "ebook") + "_library_id"] = None
    if medium == "ebook":
        values.update(abridged=None, required_narrators=[])
    return RequestSpec.model_validate(values)


async def preview(db, user, selection_id):
    selection = await acquisition_selection.owned_selection(db, user, selection_id)
    from app.domain.recovery_approvals import require_current

    await require_current(db, "selection", selection.id)
    if selection.state != "prepared" or selection.frozen.get("automatic_selection"):
        raise HTTPException(409, "Use an uncommitted manual release selection for this review")
    if not await acquisition_selection.configuration_current(db, selection):
        raise HTTPException(409, "The saved release or route changed; prepare a new selection")
    intent = await db.get(AcquisitionIntent, selection.intent_id)
    work = await canonical_work(db, intent.work_id)
    if str(work.id) != selection.frozen["work_id"]:
        raise HTTPException(409, "Book identity changed; prepare a new selection")
    destination = await db.get(ImportDestination, selection.destination_id)
    spec = child_specification(intent, selection, destination)
    profile = ProfileSnapshot.model_validate(selection.frozen["profile"])
    current_profile = await refresh_profile(db, user.id, profile)
    current_profile = current_profile.model_copy(
        update={
            "preferences": constrained_preferences(
                current_profile.preferences, selection.frozen["requirements"]
            )
        }
    )
    if not same_profile(profile, current_profile):
        raise HTTPException(
            409, "Saved preferences changed; prepare a new selection before expanding its scope"
        )
    await acquisition.validate_request(db, user, work.id, spec)
    root_spec = RequestSpec.model_validate(intent.specification)
    outcomes = await acquisition.assess(db, user, work.id, root_spec)
    if next(t for t in outcomes if t["slot"] == selection.frozen["slot"])[
        "state"
    ] != "wanted" or not await db.scalar(
        select(AcquisitionReason.id)
        .where(AcquisitionReason.intent_id == intent.id, AcquisitionReason.active.is_(True))
        .limit(1)
    ):
        raise HTTPException(409, "The original book is no longer wanted; refresh its request")
    result = {
        "selection_id": str(selection.id),
        "state": "review",
        "message": "This release needs a reviewed main-book set before preparing extra books",
        "medium": spec.mode,
        "external_id": None,
        "records": [],
    }
    if not profile.preferences.allows_series_packs:
        return {**result, "message": "This selection's profile is limited to a single book"}
    planned = await list_series.plan(db, user, work.id)
    result.update(message=planned["message"], external_id=planned.get("external_id"))
    if planned["state"] != "ready":
        return result
    artifact = await db.get(SourceArtifact, selection.artifact_id)
    release = parse_release(artifact.source_key, artifact.release_snapshot)
    if artifact.descriptor.get("protocol") == "nzb":
        raise HTTPException(422, "Usenet releases are imported after the download finishes")
    descriptor = TorrentDescriptor.model_validate(artifact.descriptor)
    artifact_bytes(artifact)
    if (
        artifact.sha256 != selection.frozen["artifact_sha256"]
        or descriptor.artifact_sha256 != artifact.sha256
        or descriptor.model_dump(mode="json") != selection.frozen["descriptor"]
        or release.model_dump(mode="json") != selection.frozen["release"]
    ):
        raise HTTPException(409, "The saved pack artifact changed; inspect and select it again")
    context = await pack_coverage.catalog(db, user, work)
    coverage = pack_coverage.manifest(
        release,
        {"id": str(work.id), "title": work.title, "authors": work.authors},
        context,
        descriptor,
        spec.mode,
    )
    if not coverage or coverage["series_id"] != planned["series_id"]:
        return {**result, "message": "The torrent manifest does not establish this reviewed series"}
    ids = {r["work"]["id"] for r in coverage["members"]} - {str(work.id)}
    records = []
    for record in planned["records"]:
        if record["work_id"] not in ids:
            continue
        work_id = UUID(record["work_id"])
        await acquisition.validate_request(db, user, work_id, spec)
        targets = await pending_targets(
            db, user, work_id, spec, await acquisition.assess(db, user, work_id, spec)
        )
        records.append(
            {
                "work_id": record["work_id"],
                "title": record["title"],
                "authors": record["authors"],
                "state": targets[0]["state"],
                "message": targets[0]["message"],
            }
        )
    evidence = {
        "selection_id": str(selection.id),
        "artifact_sha256": artifact.sha256,
        "scope": planned,
        "coverage": coverage,
        "specification": spec.model_dump(mode="json"),
        "profile": selection.frozen["profile"],
        "records": records,
    }
    return {
        **result,
        "state": "ready" if records else "empty",
        "message": "Review additional covered books before preparing their selections"
        if records
        else "No additional reviewed main books are covered by this pack",
        "revision": fingerprint(evidence),
        "records": records,
        "_evidence": evidence,
    }


async def prepare(db, user, selection_id, revision, work_ids, key):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Release preparation is paused for recovery")
    ids = sorted(set(map(str, work_ids)))
    if not ids or len(ids) > 20 or len(ids) != len(work_ids):
        raise HTTPException(422, "Choose 1–20 distinct covered books")
    command = {"selection_id": str(selection_id), "revision": revision, "work_ids": ids}
    batch = fingerprint(command)
    await transaction_lock(db, f"operation:{user.id}:{key}")
    previous = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if previous:
        if previous.kind != KIND or previous.payload["command"] != command:
            raise HTTPException(409, "This preparation key belongs to a different pack review")
        return previous.payload["receipt"]
    await transaction_lock(db, f"manual-pack:{selection_id}")
    accepted = await db.scalar(
        select(Operation)
        .where(
            Operation.owner_id == user.id,
            Operation.kind == KIND,
            Operation.payload["command"] == command,
        )
        .limit(1)
    )
    if accepted:
        db.add(
            Operation(
                owner_id=user.id,
                kind=KIND,
                status="completed",
                idempotency_key=key,
                message=accepted.message,
                payload=accepted.payload,
            )
        )
        await db.flush()
        return accepted.payload["receipt"]
    initial = await preview(db, user, selection_id)
    if initial.get("revision") != revision or initial["state"] != "ready":
        raise HTTPException(409, "Pack review changed; refresh its scope before preparing")
    by_id = {r["work_id"]: r for r in initial["records"]}
    if not set(ids) <= by_id.keys():
        raise HTTPException(422, "Choose only the additional books in this pack review")
    for identifier in ids:
        for action in ("request", "selection"):
            await transaction_lock(db, f"operation:{user.id}:pack-{action}:{batch}:{identifier}")
    await graph_lock(db)
    root = await acquisition_selection.owned_selection(db, user, selection_id)
    roots = {UUID(root.frozen["work_id"]), *map(UUID, ids)}
    for work_id in sorted(roots):
        await acquisition_lock(db, work_id)
    current = await preview(db, user, selection_id)
    if current.get("revision") != revision:
        raise HTTPException(409, "Book availability or scope changed; refresh the pack review")
    evidence = current["_evidence"]
    spec = RequestSpec.model_validate(evidence["specification"])
    scope = evidence["scope"]
    now = datetime.now(UTC)
    parent = Operation(
        owner_id=user.id,
        kind="series.requests",
        idempotency_key=f"manual-pack-scope:{batch}",
        status="running",
        message="Preparing reviewed books from the chosen pack",
        payload={
            "command": {
                "external_id": scope["external_id"],
                "work_ids": ids,
                "scope": "selected",
                "specification": spec.model_dump(mode="json"),
            },
            "series": {
                "id": scope["series_id"],
                "name": scope["series_name"],
                "external_id": scope["external_id"],
                "generation": scope["catalog_generation"],
                "fetched_at": scope["fetched_at"],
            },
            "records": [r for r in scope["records"] if r["work_id"] in ids],
            "omitted": scope["omitted"],
            "scope_review": scope["scope_review"],
            "main_membership": "user-confirmed",
            "effective_specification": spec.model_dump(mode="json"),
            "release_policy": evidence["profile"],
            "accepted_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "manual_pack_selection_id": str(selection_id),
        },
    )
    db.add(parent)
    await db.flush()
    requests = []
    records, selections = [], [root]
    for identifier in ids:
        record = by_id[identifier]
        if record["state"] not in {"wanted", "pending"}:
            records.append({**record, "selection_id": None})
            continue
        intent, _ = await acquisition.submit(
            db,
            user,
            UUID(identifier),
            spec,
            RequestReason(),
            f"pack-request:{batch}:{identifier}",
            frozen_preferences=evidence["profile"],
            series_reference=parent.id,
            hold_for_approval=False,
        )
        requests.append({"work_id": identifier, "request_id": str(intent.id)})
        target = await db.scalar(
            select(AcquisitionTarget).where(
                AcquisitionTarget.intent_id == intent.id, AcquisitionTarget.slot == spec.mode
            )
        )
        reservation = (
            await db.get(AcquisitionReservation, target.reservation_id)
            if target.reservation_id
            else None
        )
        if target.state != "wanted" or not reservation or reservation.state == "committed":
            records.append(
                {
                    **record,
                    "state": target.state if target.state != "wanted" else "pending",
                    "message": target.message,
                    "selection_id": None,
                }
            )
            continue
        selected = None
        if reservation.state == "selected":
            selected = await db.scalar(
                select(AcquisitionSelection).where(
                    AcquisitionSelection.reservation_id == reservation.id,
                    AcquisitionSelection.state == "prepared",
                    AcquisitionSelection.owner_id == user.id,
                )
            )
            if selected and selected.frozen.get("automatic_selection"):
                selected = None
            try:
                if selected:
                    require_same_transfer([root, selected])
            except HTTPException:
                selected = None
            if not selected:
                records.append(
                    {
                        **record,
                        "state": "pending",
                        "message": "Another release is already selected for this book",
                        "selection_id": None,
                    }
                )
                continue
        if not selected:
            selected = await acquisition_selection.prepare(
                db,
                user,
                SelectionInput(
                    intent_id=intent.id,
                    slot=spec.mode,
                    artifact_id=root.artifact_id,
                    downloader_id=root.downloader_id,
                    downloader_generation=root.frozen["downloader"]["generation"],
                    destination_id=root.destination_id,
                    destination_revision=fingerprint(root.frozen["destination"]),
                    confirmed_work_id=UUID(identifier),
                ),
                f"pack-selection:{batch}:{identifier}",
            )
            require_same_transfer([root, selected])
        selections.append(selected)
        records.append(
            {
                **record,
                "state": "prepared",
                "message": "Prepared for this pack; download has not started",
                "selection_id": str(selected.id),
            }
        )
    receipt = {
        "message": "Pack selections prepared; review the group before starting its download",
        "selections": [{"id": str(s.id), "title": s.frozen["work_title"]} for s in selections],
        "records": records,
        "request_id": str(parent.id),
        "external_id": scope["external_id"],
    }
    parent.status, parent.message = "completed", receipt["message"]
    parent.payload = {**parent.payload, "receipt": requests}
    db.add(
        Operation(
            owner_id=user.id,
            kind=KIND,
            status="completed",
            idempotency_key=key,
            message=receipt["message"],
            payload={"command": command, "evidence": evidence, "receipt": receipt},
        )
    )
    await db.flush()
    return receipt
