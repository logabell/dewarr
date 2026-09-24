"""Explicit operational handoff; request ownership and publication authority stay scoped."""

from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionSelection,
    AcquisitionTarget,
    AuditEvent,
    BookList,
    DownloadAttempt,
    DownloadHandoff,
    DownloadInspection,
    DownloadRecovery,
    FrozenImportPlan,
    ImportDestination,
    ImportEntry,
    ImportRun,
    Integration,
    LibraryGrant,
    ListEntry,
    Operation,
    User,
    Version,
)
from app.domain import download_memberships, narrators
from app.domain.acquisition import RequestSpec, evaluate, language_accepts, validate_request
from app.domain.downloaders import mapped_path
from app.domain.operations import transaction_lock
from app.domain.release_profiles import ProfileSnapshot, enforce_inspected_profile
from app.domain.work_graph import canonical_work, family_ids
from app.importing.naming import fingerprint
from app.importing.storage import import_sources
from app.importing.versioning import version_revision


async def assignment(db, attempt_id):
    return await db.scalar(
        select(DownloadHandoff).where(
            DownloadHandoff.attempt_id == attempt_id, DownloadHandoff.active.is_(True)
        )
    )


async def for_inspection(db, inspection_id):
    return await db.scalar(
        select(DownloadHandoff).where(DownloadHandoff.inspection_id == inspection_id)
    )


async def has_imports(db, inspection_id):
    return bool(
        await db.scalar(
            select(ImportEntry.id)
            .join(ImportRun)
            .join(FrozenImportPlan)
            .where(
                FrozenImportPlan.inspection_id == inspection_id,
                or_(ImportEntry.reserved.is_(True), ImportEntry.published_at.is_not(None)),
            )
            .limit(1)
        )
    )


async def requester_authority(db, selection, *, lock=False):
    owner = await db.get(User, selection.owner_id, populate_existing=True)
    if not owner or not owner.active or owner.role == "viewer":
        raise HTTPException(409, "The requesting account needs attention before import")
    intent = await db.get(AcquisitionIntent, selection.intent_id)
    spec = RequestSpec.model_validate(intent.specification)
    destination = await db.get(ImportDestination, selection.destination_id)
    await validate_request(
        db,
        owner,
        intent.work_id,
        spec.model_copy(
            update={
                selection.frozen["requirements"]["medium"] + "_library_id": destination.library_id,
            }
        ),
    )
    if expected := selection.frozen.get("version_identity_revision"):
        version = await db.get(
            Version, UUID(selection.frozen["requirements"]["version_id"]), populate_existing=True
        )
        if not version or version_revision(version) != expected:
            raise HTTPException(
                409, "The selected catalog version changed; review it before importing"
            )
    if owner.role != "admin":
        grant_query = select(LibraryGrant).where(
            LibraryGrant.user_id == owner.id, LibraryGrant.library_id == destination.library_id
        )
        if lock:
            grant_query = grant_query.with_for_update(read=True)
        if not await db.scalar(grant_query):
            raise HTTPException(409, "The requester no longer has access to this library")
    query = select(AcquisitionReason).where(
        AcquisitionReason.intent_id == intent.id, AcquisitionReason.active.is_(True)
    )
    if lock:
        query = query.with_for_update(read=True)
    reasons = list(await db.scalars(query))
    for reason in reasons:
        if reason.kind in {"manual", "series"} or await db.scalar(
            select(BookList.id)
            .join(ListEntry)
            .where(
                BookList.id == reason.list_id,
                BookList.owner_id == owner.id,
                ListEntry.work_id.in_(family_ids(intent.work_id)),
            )
            .limit(1)
        ):
            return owner, intent, destination
    raise HTTPException(409, "The request was withdrawn; review its acquisition before importing")


async def requesting_selection(db, attempt, representative):
    members = await download_memberships.for_attempt(db, attempt.id)
    if len(members) <= 1:
        return representative
    for item in members:
        target = await db.get(AcquisitionTarget, item.target_id, populate_existing=True)
        if item.state != "committed" or target.state != "wanted":
            continue
        try:
            await requester_authority(db, item)
        except HTTPException:
            continue
        return item
    raise HTTPException(409, "No authorized book in this transfer currently needs import")


def validate_version(rule, version, inspection, group):
    if version.medium != rule["medium"] or not language_accepts(rule["language"], version.language):
        raise HTTPException(422, "This version does not satisfy the requested medium or language")
    if rule["abridged"] is not None and version.abridged != rule["abridged"]:
        raise HTTPException(422, "This recording does not satisfy the abridgment requirement")
    if rule["version_id"] and str(version.id) != rule["version_id"]:
        raise HTTPException(422, "Select the requested edition or recording for this book")
    required = rule.get("required_narrators", [])
    if not narrators.accepts(required, version.narrators):
        raise HTTPException(422, "This recording does not confirm every required narrator")
    if required and group is not None:
        from app.importing.match_evidence import group_evidence

        facts = group_evidence(inspection.snapshot, group)
        if not facts.narrators or any(
            not narrators.accepts(required, names) for names in facts.narrators
        ):
            raise HTTPException(422, "Inspected audio does not confirm every required narrator")


async def validate_shared_inspection(
    db, attempt, members, *, destination_id, version, group, lock, publication=False
):
    inspection = await db.get(DownloadInspection, attempt.inspection_id)
    for item in members:
        if inspection.snapshot and item.frozen.get("recovery_profile"):
            enforce_inspected_profile(
                inspection.snapshot["files"],
                ProfileSnapshot.model_validate(item.frozen["recovery_profile"]),
            )
        if inspection.snapshot and item.frozen.get("profile"):
            enforce_inspected_profile(
                inspection.snapshot["files"], ProfileSnapshot.model_validate(item.frozen["profile"])
            )
    handoff = await for_inspection(db, inspection.id)
    if handoff:
        reviewer = await db.get(User, handoff.reviewer_id, populate_existing=True)
        if not handoff.active or not reviewer or not reviewer.active or reviewer.role != "admin":
            raise HTTPException(409, "The assigned administrator no longer has import access")
    candidates = members
    if version:
        work_id = (await canonical_work(db, version.work_id)).id
        candidates = [
            item
            for item in members
            if (await canonical_work(db, UUID(item.frozen["origin_work_id"]))).id == work_id
        ]
    last_error = HTTPException(422, "This book is outside the reviewed transfer scope")
    for item in candidates:
        try:
            if publication:
                from app.domain.list_series import require_import_authority

                await require_import_authority(db, item)
            _, _, destination = await requester_authority(db, item, lock=lock)
            if destination_id and destination.id != destination_id:
                raise HTTPException(422, "Use the destination selected for this acquisition")
            if version:
                validate_version(item.frozen["requirements"], version, inspection, group)
            return
        except HTTPException as error:
            last_error = error
    raise last_error


async def lock_principals(db, inspection_id):
    """Acquire list/series origins and users before backend/library rows."""
    handoff = await for_inspection(db, inspection_id)
    if handoff:
        attempt = await db.get(DownloadAttempt, handoff.attempt_id)
        from app.domain.automatic_dispatch import lock_group_principals
        from app.domain.list_series import lock_import_reasons

        members = await download_memberships.for_attempt(db, attempt.id)
        await lock_group_principals(
            db,
            members,
            additional_user_ids=(handoff.reviewer_id,),
        )
        await lock_import_reasons(db, members)


async def validate_inspection(
    db, inspection_id, *, destination_id=None, version=None, group=None, lock=False
):
    """No work locks: safe under the importer's existing publication lock order."""
    attempt = await db.scalar(
        select(DownloadAttempt).where(DownloadAttempt.inspection_id == inspection_id)
    )
    if attempt:
        if await db.scalar(
            select(DownloadRecovery.id).where(DownloadRecovery.attempt_id == attempt.id).limit(1)
        ):
            raise HTTPException(409, "This release was rejected; review its replacement download")
        members = await download_memberships.for_attempt(db, attempt.id)
        if len(members) > 1:
            await validate_shared_inspection(
                db,
                attempt,
                members,
                destination_id=destination_id,
                version=version,
                group=group,
                lock=lock,
            )
            return
        selection = await db.get(AcquisitionSelection, attempt.selection_id)
        if version and not narrators.accepts(
            selection.frozen["requirements"].get("required_narrators", []), version.narrators
        ):
            raise HTTPException(422, "This recording does not confirm every required narrator")
        inspection = await db.get(DownloadInspection, inspection_id)
        required = selection.frozen["requirements"].get("required_narrators", [])
        if required and group is not None:
            from app.importing.match_evidence import group_evidence

            facts = group_evidence(inspection.snapshot, group)
            if not facts.narrators or any(
                not narrators.accepts(required, names) for names in facts.narrators
            ):
                raise HTTPException(422, "Inspected audio does not confirm every required narrator")
        if inspection.snapshot and selection.frozen.get("recovery_profile"):
            enforce_inspected_profile(
                inspection.snapshot["files"],
                ProfileSnapshot.model_validate(selection.frozen["recovery_profile"]),
            )
        if inspection.snapshot and selection.frozen.get("profile"):
            enforce_inspected_profile(
                inspection.snapshot["files"],
                ProfileSnapshot.model_validate(selection.frozen["profile"]),
            )
    handoff = await for_inspection(db, inspection_id)
    if not handoff:
        return
    attempt = await db.get(DownloadAttempt, handoff.attempt_id)
    if not handoff.active or attempt.inspection_id != inspection_id:
        raise HTTPException(
            409, "This review was reassigned; open the current administrator review"
        )
    selection = await db.get(AcquisitionSelection, attempt.selection_id)
    reviewer = await db.get(User, handoff.reviewer_id, populate_existing=True)
    if not reviewer or not reviewer.active or reviewer.role != "admin":
        raise HTTPException(409, "The assigned administrator no longer has import access")
    _, _, destination = await requester_authority(db, selection, lock=lock)
    if destination_id and destination_id != destination.id:
        raise HTTPException(422, "Use the destination selected for this acquisition")
    if version:
        rule = selection.frozen["requirements"]
        if version.medium != rule["medium"] or not language_accepts(
            rule["language"], version.language
        ):
            raise HTTPException(
                422, "This version does not satisfy the requested medium or language"
            )
        if rule["abridged"] is not None and version.abridged != rule["abridged"]:
            raise HTTPException(422, "This recording does not satisfy the abridgment requirement")
        work = await canonical_work(db, UUID(selection.frozen["origin_work_id"]))
        if (
            (await canonical_work(db, version.work_id)).id == work.id
            and rule["version_id"]
            and str(version.id) != rule["version_id"]
        ):
            raise HTTPException(422, "Select the edition or recording requested by this member")


async def queue_view(db, admin, attempt, selection):
    handoff = await assignment(db, attempt.id)
    inspection = await db.get(DownloadInspection, handoff.inspection_id) if handoff else None
    retry = bool(inspection and inspection.state == "failed" and handoff.reviewer_id == admin.id)
    revision = fingerprint(
        {
            "attempt_id": str(attempt.id),
            "inspection_id": str(attempt.inspection_id) if attempt.inspection_id else None,
            "assignment_id": str(handoff.id) if handoff else None,
            "inspection_state": inspection.state if inspection else None,
        }
    )
    message, allowed = "Ready for administrator inspection", True
    try:
        active = await requesting_selection(db, attempt, selection)
        await requester_authority(db, active)
    except HTTPException:
        message, allowed = "Requester access or active request needs attention", False
    if handoff:
        if allowed:
            message = (
                "Assigned to you"
                if handoff.reviewer_id == admin.id
                else "Assigned to another administrator"
            )
        if await has_imports(db, handoff.inspection_id):
            if allowed:
                message = "Import underway or published; continue its existing review"
            allowed = False
        elif allowed and retry:
            message = "Inspection failed; correct the reported issue and inspect again"
    elif attempt.inspection_id:
        allowed, message = False, "Inspection is already owned by the original requester"
    return {
        "attempt_id": attempt.id,
        "work_title": selection.frozen["work_title"],
        "medium": selection.frozen["requirements"]["medium"],
        "message": message,
        "revision": revision,
        "inspection_id": handoff.inspection_id
        if handoff and handoff.reviewer_id == admin.id
        else None,
        "can_claim": allowed and (not handoff or handoff.reviewer_id != admin.id or retry),
        "reassignment": bool(handoff),
        "retry": retry,
    }


async def claim(db, admin, identifier, revision, key):
    from app.api.imports import assert_admin
    from app.domain.download_attempts import create_inspection, locked

    if get_settings().recovery_mode:
        raise HTTPException(409, "Download review is paused for recovery")
    await transaction_lock(db, f"operation:{admin.id}:{key}")
    await assert_admin(db, admin.id)
    receipt = await db.scalar(
        select(Operation).where(Operation.owner_id == admin.id, Operation.idempotency_key == key)
    )
    if receipt:
        if (
            receipt.kind != "acquisition.review"
            or receipt.payload.get("attempt_id") != str(identifier)
            or receipt.payload.get("revision") != revision
        ):
            raise HTTPException(409, "This command key was used for another review")
        attempt = await db.get(DownloadAttempt, identifier)
        return await queue_view(
            db, admin, attempt, await db.get(AcquisitionSelection, attempt.selection_id)
        )
    previous = await assignment(db, identifier)
    if previous:
        # Match planning/starting imports: inspection lock precedes domain locks.
        await transaction_lock(db, f"inspection-plan:{previous.inspection_id}")
    attempt, selection = await locked(db, identifier)
    if not attempt or attempt.state != "complete":
        raise HTTPException(404, "Completed acquisition awaiting review not found")
    selection = await requesting_selection(db, attempt, selection)
    if selection.state != "committed":
        raise HTTPException(404, "Completed acquisition awaiting review not found")
    handoff = await assignment(db, attempt.id)
    if attempt.inspection_id:
        if not handoff:
            raise HTTPException(409, "This download already has its own inspection")
        if not previous or previous.id != handoff.id:
            raise HTTPException(409, "Review assignment changed; refresh the queue")
        if await has_imports(db, handoff.inspection_id):
            raise HTTPException(409, "Resolve the existing import before reassigning its review")
    current = await queue_view(db, admin, attempt, selection)
    if current["revision"] != revision:
        raise HTTPException(409, "Review assignment changed; refresh the queue")
    if handoff and handoff.reviewer_id == admin.id and not current["retry"]:
        raise HTTPException(409, "This review is already assigned to you")
    owner, intent, _ = await requester_authority(db, selection)
    await evaluate(db, owner, intent)
    target = await db.get(AcquisitionTarget, selection.target_id)
    if target.state != "wanted":
        raise HTTPException(409, "This request is no longer missing the selected media")
    downloader = await db.get(Integration, selection.downloader_id)
    if (
        mapped_path(
            downloader, selection.frozen["downloader"]["save_path"], await import_sources(db)
        )
        != selection.frozen["mapping"]
    ):
        raise HTTPException(
            409, "The completed download mapping changed; reconcile its file location"
        )
    if handoff:
        handoff.active = False
        await db.flush()
    handoff_id = uuid4()
    inspection = await create_inspection(
        db, attempt, selection, admin, key="download-review-inspection:" + str(handoff_id)
    )
    receipt = Operation(
        owner_id=admin.id,
        kind="acquisition.review",
        idempotency_key=key,
        status="completed",
        message="Completed download assigned for administrator import review",
        payload={
            "attempt_id": str(identifier),
            "revision": revision,
            "handoff_id": str(handoff_id),
        },
    )
    db.add(receipt)
    await db.flush()
    db.add(
        DownloadHandoff(
            id=handoff_id,
            attempt_id=attempt.id,
            reviewer_id=admin.id,
            inspection_id=inspection.id,
            operation_id=receipt.id,
        )
    )
    attempt.message = "Download complete; an administrator is reviewing its import"
    (await db.get(Operation, attempt.operation_id)).message = attempt.message
    db.add(
        AuditEvent(
            actor_id=admin.id,
            action="acquisition.review.assigned",
            entity_id=attempt.id,
            detail={
                "handoff_id": str(handoff_id),
                "previous_handoff_id": str(handoff.id) if handoff else None,
            },
        )
    )
    await db.flush()
    return await queue_view(db, admin, attempt, selection)
