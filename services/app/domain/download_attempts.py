"""Durable, one-submission download attempts. Redelivery observes, never re-adds."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, update

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.deluge import DelugeClient
from app.adapters.nzbget import NzbClient
from app.adapters.nzbget import verify_association as verify_nzb
from app.adapters.qbittorrent import QbitClient, absolute_path, verify_association
from app.adapters.sabnzbd import SabClient
from app.adapters.sabnzbd import verify_association as verify_sab
from app.adapters.torrent_descriptor import TorrentDescriptor
from app.adapters.transmission import TransmissionClient
from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    AuditEvent,
    DownloadAttempt,
    DownloadCapacity,
    DownloadIdentityClaim,
    DownloadInspection,
    DownloadMembership,
    DownloadRepair,
    ImportDestination,
    Integration,
    Operation,
    SourceArtifact,
    User,
)
from app.db.session import session_factory
from app.domain import automatic_dispatch, capacity, download_memberships
from app.domain.acquisition import RequestSpec, evaluate, validate_request
from app.domain.acquisition_selection import configuration_current, owned_selection
from app.domain.downloaders import SETTINGS_LOCK, relative_to
from app.domain.operations import transaction_lock
from app.domain.release_profiles import DEFAULTS_LOCK
from app.domain.source_artifacts import artifact_bytes, member
from app.importing.naming import fingerprint
from app.importing.storage import import_sources
from app.jobs.queue import enqueue
from app.security import decrypt_secrets

LEASE_SECONDS = 240
NETWORK_SECONDS = 180
TERMINAL = {"complete", "cancelled"}


def hashes(selection):
    descriptor = TorrentDescriptor.model_validate(selection.frozen["descriptor"])
    return {value for value in (descriptor.infohash_v1, descriptor.infohash_v2) if value}


def identities(selection):
    descriptor = selection.frozen["descriptor"]
    if descriptor.get("parser") == "slskd-file-list" or descriptor.get("protocol") == "nzb":
        digest = descriptor.get("artifact_sha256")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            return {digest}
        return set()
    return hashes(selection)


def nzb_release(selection):
    return selection.frozen["descriptor"].get("protocol") == "nzb"


def attempt_tag(attempt):
    return "book-search:" + str(attempt.id)


def inspection_path(selection):
    descriptor = selection.frozen["descriptor"]
    paths = [item["path"] for item in descriptor["files"]]
    if len(paths) == 1 and "/" not in paths[0]:
        name = paths[0]
    elif all(path.startswith(descriptor["name"] + "/") for path in paths):
        name = descriptor["name"]
    else:
        raise HTTPException(409, "Torrent files do not have one supported inspection root")
    relative = "/".join(
        part
        for part in (selection.frozen["mapping"]["relative_path"], name)
        if part not in {"", "."}
    )
    if len(relative) > 1024:
        raise HTTPException(409, "Completed download path exceeds the inspection limit")
    return relative


async def locked(db, identifier):
    await transaction_lock(db, f"download-members:{identifier}")
    attempt = await db.get(DownloadAttempt, identifier)
    if not attempt:
        return None, None
    selection = await db.get(AcquisitionSelection, attempt.selection_id)
    await download_memberships.lock(db, await download_memberships.for_attempt(db, attempt.id))
    await db.refresh(attempt, with_for_update=True)
    await db.refresh(selection)
    return attempt, selection


async def owned_attempt(db, user, identifier):
    row = await db.get(DownloadAttempt, identifier, populate_existing=True)
    if not row or row.owner_id != user.id:
        raise HTTPException(404, "Download attempt not found")
    return row


async def selection_authority(db, selection, *, wanted, configuration=None, dispatch_consent=True):
    """Recheck current grants and frozen configuration at the side-effect boundary."""
    if get_settings().recovery_mode:
        raise HTTPException(409, "Downloads are paused for recovery")
    if wanted and dispatch_consent and not get_settings().download_dispatch_enabled:
        raise HTTPException(409, "New download dispatch is disabled")
    await member(db, selection.owner_id)
    from app.domain.recovery_approvals import require_current

    await require_current(db, "selection", selection.id)
    user = await db.get(User, selection.owner_id)
    intent = await db.get(AcquisitionIntent, selection.intent_id)
    from app.domain import release_blocklist

    if wanted and await release_blocklist.blocked(
        db,
        intent.work_id,
        selection.frozen["requirements"]["medium"],
        selection.frozen["release"],
        selection.frozen["descriptor"],
    ):
        raise HTTPException(409, "This release is blocklisted for this book and medium")
    if wanted:
        await evaluate(db, user, intent)
        await db.flush()
        target = await db.get(AcquisitionTarget, selection.target_id, populate_existing=True)
        if target.state != "wanted" or target.reservation_id != selection.reservation_id:
            raise HTTPException(409, "The selected target is no longer wanted")
    source_artifact = await db.get(SourceArtifact, selection.artifact_id)
    await transaction_lock(db, f"source:{source_artifact.source_key}")
    await transaction_lock(db, SETTINGS_LOCK)
    destination = await db.get(ImportDestination, selection.destination_id, with_for_update=True)
    spec = RequestSpec.model_validate(intent.specification)
    medium = selection.frozen["requirements"]["medium"]
    await validate_request(
        db,
        user,
        intent.work_id,
        spec.model_copy(
            update={
                medium + "_library_id": destination.library_id,
            }
        ),
        check_version_constraints=wanted,
    )
    if configuration is None:
        from app.domain.download_repairs import accepted_configuration

        configuration = await accepted_configuration(db, selection)
    if not await configuration_current(
        db, selection, committed=True, configuration=configuration, version_identity_required=wanted
    ):
        raise HTTPException(409, "Saved acquisition settings changed; review the download route")
    artifact = await db.get(SourceArtifact, selection.artifact_id)
    content = artifact_bytes(artifact)
    if (
        artifact.sha256 != selection.frozen["artifact_sha256"]
        or artifact.descriptor != selection.frozen["descriptor"]
    ):
        raise HTTPException(409, "Saved torrent identity changed; inspect the release again")
    if wanted and not nzb_release(selection):
        inspection_path(selection)
    if wanted and dispatch_consent:
        await automatic_dispatch.require_selection(db, selection)
    return await db.get(Integration, selection.downloader_id), content


async def authority(db, selection, *, wanted, configuration=None, dispatch_consent=True):
    attempt = await download_memberships.attempt_for(db, selection.id)
    members = await download_memberships.for_attempt(db, attempt.id) if attempt else [selection]
    if len(members) == 1 or not wanted:
        return await selection_authority(
            db,
            selection,
            wanted=wanted,
            configuration=configuration,
            dispatch_consent=dispatch_consent,
        )
    active = await wanted_members(db, members)
    if not active:
        raise HTTPException(409, "None of the selected book targets is still wanted")
    result = None
    for item in active:
        result = await selection_authority(
            db,
            item,
            wanted=True,
            configuration=configuration,
            dispatch_consent=dispatch_consent,
        )
    return result


async def wanted_members(db, members):
    active = []
    for item in members:
        user = await db.get(User, item.owner_id)
        await evaluate(db, user, await db.get(AcquisitionIntent, item.intent_id))
        await db.flush()
        target = await db.get(AcquisitionTarget, item.target_id, populate_existing=True)
        if target.state == "wanted" and target.reservation_id == item.reservation_id:
            active.append(item)
    return active


async def start(
    db,
    user,
    selection_id,
    key,
    *,
    automatic=False,
    additional_selection_ids=(),
    attempt_id=None,
    already_queued=False,
):
    additional = sorted(set(additional_selection_ids))
    if len(additional) > 99 or selection_id in additional:
        raise HTTPException(422, "Choose up to 100 distinct selections for one transfer")
    additional_keys = [str(identifier) for identifier in additional]
    await transaction_lock(db, f"operation:{user.id}:{key}")
    await member(db, user.id)
    receipt = await db.scalar(
        select(Operation).where(
            Operation.owner_id == user.id,
            Operation.idempotency_key == key,
        )
    )
    if receipt:
        if (
            receipt.kind != "acquisition.download"
            or receipt.payload.get("selection_id") != str(selection_id)
            or receipt.payload.get("automatic", False) != automatic
            or receipt.payload.get("additional_selection_ids", []) != additional_keys
        ):
            raise HTTPException(409, "This command key was already used for another operation")
        return await owned_attempt(db, user, UUID(receipt.payload["attempt_id"]))
    if not get_settings().download_dispatch_enabled:
        raise HTTPException(409, "Download dispatch is not enabled for this installation")
    selection = await owned_selection(db, user, selection_id)
    members = [selection] + [
        await owned_selection(db, user, identifier) for identifier in additional
    ]
    if additional:
        download_memberships.require_compatible(members, automatic=automatic)
    await download_memberships.lock(db, members)
    existing = await download_memberships.attempt_for(db, selection.id)
    if existing and additional:
        saved = {item.id for item in await download_memberships.for_attempt(db, existing.id)}
        if saved != {item.id for item in members}:
            raise HTTPException(409, "This transfer already has a different frozen book scope")
    if existing:
        # Every accepted command key gets its own durable receipt, including aliases.
        db.add(
            Operation(
                owner_id=user.id,
                kind="acquisition.download",
                idempotency_key=key,
                status="completed",
                message="Existing download attempt returned",
                payload={
                    "selection_id": str(selection.id),
                    "attempt_id": str(existing.id),
                    "automatic": automatic,
                    **({"additional_selection_ids": additional_keys} if additional else {}),
                },
            )
        )
        return existing
    if selection.frozen.get("download_recovery"):
        from app.domain.download_recovery import configuration, history

        if len(await history(db, selection)) >= (await configuration(db)).attempt_cap:
            raise HTTPException(409, "The replacement attempt limit has been reached")
    if not automatic and any(automatic_dispatch.consent(item) for item in members):
        raise HTTPException(409, "Automatic selections must use their authorized dispatch workflow")
    for item in members:
        if item.state != "prepared" or await download_memberships.attempt_for(db, item.id):
            raise HTTPException(
                409, "Select current uncommitted releases before grouping a download"
            )
        await selection_authority(db, item, wanted=True)
    downloader = await db.get(Integration, selection.downloader_id)
    endpoint_key = fingerprint({"url": downloader.base_url.rstrip("/")})
    claimed = identities(selection)
    if not claimed:
        raise HTTPException(409, "Download identity is required before dispatch")
    # Endpoint-scoped claims also catch two saved connections to the same URL.
    for digest in sorted(claimed):
        await transaction_lock(db, f"download-identity:{endpoint_key}:{digest}")
    if await db.scalar(
        select(DownloadIdentityClaim.id)
        .where(
            DownloadIdentityClaim.endpoint_key == endpoint_key,
            DownloadIdentityClaim.torrent_hash.in_(claimed),
            DownloadIdentityClaim.active.is_(True),
        )
        .limit(1)
    ):
        raise HTTPException(
            409, "This transfer is already recorded; its existing files need reconciliation"
        )
    if already_queued and attempt_id is None:
        raise HTTPException(409, "A queued Soulseek batch needs its attempt id")
    attempt_id, operation_id = attempt_id or uuid4(), uuid4()
    operation = Operation(
        id=operation_id,
        owner_id=user.id,
        kind="acquisition.download",
        integration_id=downloader.id,
        idempotency_key=key,
        payload={
            "selection_id": str(selection.id),
            "attempt_id": str(attempt_id),
            "automatic": automatic,
            **({"additional_selection_ids": additional_keys} if additional else {}),
        },
    )
    db.add(operation)
    await db.flush()
    attempt = DownloadAttempt(
        id=attempt_id,
        owner_id=user.id,
        selection_id=selection.id,
        operation_id=operation.id,
        endpoint_key=endpoint_key,
        next_check_at=datetime.now(UTC),
        state="submitting" if already_queued else "queued",
        external_may_exist=already_queued,
        message=(
            "Soulseek batch queued; waiting for the transfer"
            if already_queued
            else "Waiting to check the downloader"
        ),
    )
    db.add(attempt)
    await db.flush()
    db.add_all(
        [DownloadMembership(attempt_id=attempt.id, selection_id=item.id) for item in members]
    )
    db.add(DownloadCapacity(attempt_id=attempt.id, automatic=automatic))
    db.add_all(
        [
            DownloadIdentityClaim(
                attempt_id=attempt.id, endpoint_key=endpoint_key, torrent_hash=digest
            )
            for digest in sorted(claimed)
        ]
    )
    for item in members:
        reservation = await db.get(AcquisitionReservation, item.reservation_id)
        reservation.state, item.state = "committed", "committed"
        item.message = "Download queued; follow its progress in Activity"
    operation.job_id = await enqueue(db, "acquisition.download", attempt_id=str(attempt.id))
    db.add(AuditEvent(actor_id=user.id, action="acquisition.download.queued", entity_id=attempt.id))
    return attempt


async def record(db, attempt, state, message, *, poll=False):
    attempt.state, attempt.message = state, message
    attempt.run_token, attempt.lease_until = None, None
    attempt.next_check_at = datetime.now(UTC) + timedelta(seconds=60) if poll else None
    operation = await db.get(Operation, attempt.operation_id)
    operation.status = (
        "completed"
        if state in TERMINAL
        else "held"
        if state in {"held", "uncertain"}
        else "running"
    )
    operation.message = message
    if not attempt.external_may_exist:
        await capacity.release_unsubmitted(db, attempt)


async def cancel(db, user, identifier):
    await owned_attempt(db, user, identifier)
    attempt, selection = await locked(db, identifier)
    await member(db, user.id)
    if attempt.state == "cancelled":
        return attempt
    if attempt.external_may_exist:
        raise HTTPException(
            409, "Submission may have reached the downloader; reconcile it instead of cancelling"
        )
    await record(db, attempt, "cancelled", "Cancelled before submission; no torrent was removed")
    members = await download_memberships.for_attempt(db, attempt.id)
    for item in members:
        item.state, item.message = "cancelled", attempt.message
        reservation = await db.get(AcquisitionReservation, item.reservation_id)
        reservation.state = "planned"
    await db.execute(
        update(DownloadIdentityClaim)
        .where(
            DownloadIdentityClaim.attempt_id == attempt.id,
        )
        .values(active=False)
    )
    for item in members:
        intent = await db.get(AcquisitionIntent, item.intent_id)
        await evaluate(db, user, intent)
    db.add(
        AuditEvent(actor_id=user.id, action="acquisition.download.cancelled", entity_id=attempt.id)
    )
    return attempt


async def recheck(db, user, identifier):
    await owned_attempt(db, user, identifier)
    attempt, selection = await locked(db, identifier)
    await member(db, user.id)
    if get_settings().recovery_mode:
        raise HTTPException(409, "Downloads are paused for recovery")
    if attempt.state == "cancelled":
        return attempt
    from app.domain.download_repairs import latest

    if await latest(db, attempt.id, "pending"):
        raise HTTPException(409, "A reviewed connection repair is already pending")
    now = datetime.now(UTC)
    if (attempt.lease_until and attempt.lease_until > now) or (
        attempt.next_check_at and attempt.next_check_at > now
    ):
        raise HTTPException(409, "A download check is running or cooling down")
    if attempt.state == "complete":
        from app.domain.download_fulfillment import reconcile_work
        from app.importing.reuse import recheck as recheck_reuse

        await recheck_reuse(db, attempt)

        for item in await download_memberships.for_attempt(db, attempt.id):
            await reconcile_work(db, UUID(item.frozen["origin_work_id"]))
        attempt.next_check_at = now + timedelta(seconds=60)
        return attempt
    from sqlalchemy import text

    operation = await db.get(Operation, attempt.operation_id)
    status = await db.scalar(
        text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
        {"id": operation.job_id},
    )
    if status not in {"todo", "doing"}:
        operation.job_id = await enqueue(db, "acquisition.download", attempt_id=str(attempt.id))
    attempt.next_check_at = now + timedelta(seconds=60)
    return attempt


async def find(client, selection, tag):
    if nzb_release(selection):
        return await client.find(attempt_tag=tag, torrent_hash=None)
    found = {}
    for digest in sorted(hashes(selection)):
        for state in await client.find(attempt_tag=tag, torrent_hash=digest):
            found[state.external_id] = state
    return list(found.values())


def transfer_stage(selection, state):
    """Classify verified transfer evidence without scheduling import or changing state."""
    if selection is not None and nzb_release(selection):
        if getattr(state, "failed", False):
            return "held", "The Usenet client reported a failed download"
        if not state.completed:
            return "downloading", "Transfer associated; waiting for the Usenet client to finish"
        return (
            "complete",
            "Download complete; file inspection and library confirmation are still required",
        )
    if state.state == "failed":
        return "held", "Soulseek stopped this folder before every file finished"
    if not state.completed and not state.reported_complete:
        return "downloading", "Transfer associated; waiting for complete files"
    expected = {
        item["path"]: item["size_bytes"] for item in selection.frozen["descriptor"]["files"]
    }
    actual = {item.relative_path: item.size_bytes for item in state.files}
    if expected != actual or state.total_bytes != selection.frozen["descriptor"]["torrent_bytes"]:
        inspected = (
            "Soulseek file list"
            if selection.frozen["descriptor"].get("parser") == "slskd-file-list"
            else "inspected torrent"
        )
        return "held", f"Completed files differ from the {inspected}; review the downloader"
    return (
        "complete",
        "Download complete; file inspection and library confirmation are still required",
    )


async def finish_observation(db, attempt, selection, state):
    from app.domain import download_recovery

    policy = download_recovery.policy_for(await download_recovery.configuration(db), selection)
    health, failure = download_recovery.observe(
        attempt.recovery_observation, state, policy, datetime.now(UTC)
    )
    attempt.recovery_observation = health
    if failure:
        attempt.observation = state.model_dump(mode="json")
        await download_recovery.failed(db, attempt, failure)
        # A dead/error transfer is no longer an active Dewarr acquisition. Keep
        # storage reservations when a stalled transfer is left running.
        if state.state == "failed":
            await capacity.release_slot(db, attempt)
        return
    if not state:
        await record(
            db,
            attempt,
            "uncertain",
            "Submission is not yet visible; only checking for the existing transfer",
            poll=True,
        )
        return
    attempt.observation = state.model_dump(mode="json")
    next_state, message = transfer_stage(selection, state)
    if (
        next_state == "held"
        and policy.enabled
        and (state.completed or getattr(state, "reported_complete", False))
    ):
        await download_recovery.failed(db, attempt, message)
        return
    await record(db, attempt, next_state, message, poll=next_state == "downloading")
    if state.state == "failed":
        await capacity.release_slot(db, attempt)
    if next_state == "held" and state.state == "failed" and policy.enabled:
        # Continue verified failure observations until the configured grace expires.
        attempt.next_check_at = datetime.now(UTC) + timedelta(seconds=60)
    if next_state != "complete":
        return
    members = await download_memberships.for_attempt(db, attempt.id)
    for item in members:
        await enqueue(db, "acquisition.fulfillment", work_id=item.frozen["origin_work_id"])
    user = await db.get(User, attempt.owner_id)
    active = await wanted_members(db, members)
    if not active:
        paused = any(
            [
                (await db.get(AcquisitionTarget, item.target_id)).state == "paused"
                for item in members
            ]
        )
        attempt.message = (
            "Download complete; request metadata or access needs review before import"
            if paused
            else "Download complete; no selected book currently needs import"
        )
        (await db.get(Operation, attempt.operation_id)).message = attempt.message
        return False
    # The transport representative remains immutable; continuation may be needed
    # only for a different member after the first book became available.
    selection = active[0]
    from app.importing.automatic import schedule

    if await schedule(db, attempt, selection):
        return True
    # Existing reviewed import UI is administrator-only. Do not silently elevate
    # a member's source-directory access or manufacture library availability.
    if user.role != "admin":
        attempt.message = (
            "Download complete; administrator inspection is required before library import"
        )
        (await db.get(Operation, attempt.operation_id)).message = attempt.message
        return True
    await authority(db, selection, wanted=True, dispatch_consent=False)
    await create_inspection(db, attempt, selection, user)
    return True


def completed_nzb_path(selection, storage):
    root = absolute_path(selection.frozen["downloader"]["save_path"])
    extra = relative_to(absolute_path(storage), root)
    if not extra:
        raise HTTPException(
            409, "The Usenet client did not report a folder inside the download path"
        )
    relative = "/".join(
        part
        for part in (selection.frozen["mapping"]["relative_path"], extra)
        if part not in {"", "."}
    )
    if not relative or len(relative) > 1024:
        raise HTTPException(409, "Completed download path exceeds the inspection limit")
    return relative


async def create_inspection(db, attempt, selection, user, *, key=None):
    mapping = selection.frozen["mapping"]
    relative = (
        completed_nzb_path(selection, (attempt.observation or {})["save_path"])
        if nzb_release(selection)
        else inspection_path(selection)
    )
    operation = Operation(
        owner_id=user.id,
        kind="organization.inspect",
        idempotency_key=key or "download-inspection:" + str(attempt.id),
    )
    db.add(operation)
    await db.flush()
    inspection = DownloadInspection(
        owner_id=user.id,
        operation_id=operation.id,
        source_key=mapping["source_key"],
        source_path=str((await import_sources(db))[mapping["source_key"]]),
        relative_path=relative,
    )
    db.add(inspection)
    await db.flush()
    attempt.inspection_id = inspection.id
    operation.job_id = await enqueue(db, "organization.inspect", operation_id=str(operation.id))
    return inspection


async def observe_soulseek(
    identifier, token, *, endpoint, api_key, frozen, already_submitted, repair_id
):
    """Queue or watch one slskd batch. The attempt id is the batch id and external id."""
    from app.adapters.slskd import SlskdClient, SlskdRelease
    from app.domain.download_repairs import finish as finish_repair
    from app.domain.download_repairs import require_repair_actor

    release = SlskdRelease.model_validate(frozen["release"])
    async with asyncio.timeout(NETWORK_SECONDS), SlskdClient(endpoint, api_key) as client:
        if not already_submitted:
            storage = await capacity.observe_download(frozen)
            async with session_factory()() as db, db.begin():
                attempt, current = await locked(db, identifier)
                if (
                    attempt.run_token != token
                    or attempt.state != "preflight"
                    or attempt.lease_until <= datetime.now(UTC)
                ):
                    return
                if automatic_dispatch.consent(current):
                    await transaction_lock(db, DEFAULTS_LOCK)
                await authority(db, current, wanted=True)
                await capacity.admit(db, attempt, current, storage)
                await capacity.submitted(db, attempt)
                attempt.external_may_exist, attempt.state = True, "submitting"
                attempt.message = "Submission recorded; uncertain outcomes will only be reconciled"
                (await db.get(Operation, attempt.operation_id)).message = attempt.message
            await client.enqueue(release, attempt_id=str(identifier))
        try:
            state = await client.batch(str(identifier), release)
        except AdapterError as error:
            if error.kind != FailureKind.NOT_FOUND:
                raise
            raise AdapterError(
                FailureKind.UNCERTAIN,
                "Soulseek batch is not visible yet; still checking",
            ) from error
        if state.state == "failed":
            try:
                await client.cancel(release.username, str(identifier))
            except AdapterError:
                # The attempt is still recorded as held, so a missed cancel is visible.
                pass
    state.save_path = frozen["downloader"]["save_path"]
    async with session_factory()() as db, db.begin():
        attempt, current = await locked(db, identifier)
        if attempt.run_token != token:
            return
        repair = await db.get(DownloadRepair, repair_id) if repair_id else None
        if repair:
            await require_repair_actor(db, repair)
        await authority(
            db,
            current,
            wanted=False,
            configuration=repair.configuration if repair else None,
        )
        if repair and state.completed:
            await finish_repair(
                db,
                repair,
                "applied",
                "Updated connections verified against the existing transfer; no download was added",
            )
        needs_import = await finish_observation(db, attempt, current, state)
        if attempt.state == "complete":
            await capacity.downloaded(db, attempt, needs_import=bool(needs_import))
        db.add(
            AuditEvent(
                actor_id=attempt.owner_id,
                action="acquisition.download.observed",
                entity_id=attempt.id,
                detail={"state": attempt.state},
            )
        )


async def run(identifier):
    """Persist the irreversible boundary before add; all subsequent runs only find."""
    token = uuid4()
    from app.domain.download_repairs import finish as finish_repair
    from app.domain.download_repairs import latest, require_repair_actor

    async with session_factory()() as db, db.begin():
        attempt, selection = await locked(db, identifier)
        if not attempt or attempt.state in TERMINAL:
            return
        from app.db.models import DownloadRecovery

        if await db.scalar(
            select(DownloadRecovery.id).where(DownloadRecovery.attempt_id == attempt.id).limit(1)
        ):
            return
        now = datetime.now(UTC)
        if attempt.lease_until and attempt.lease_until > now:
            return
        repair = await latest(db, attempt.id, "pending")
        repair_id = repair.id if repair else None
        try:
            if repair:
                await require_repair_actor(db, repair)
            downloader, content = await authority(
                db,
                selection,
                wanted=not attempt.external_may_exist,
                configuration=repair.configuration if repair else None,
            )
        except (HTTPException, AdapterError):
            await record(
                db, attempt, "held", "Download access, request or saved settings need review"
            )
            if repair:
                await finish_repair(db, repair, "held", attempt.message)
            return
        if fingerprint({"url": downloader.base_url.rstrip("/")}) != attempt.endpoint_key:
            await record(
                db,
                attempt,
                "held",
                "Downloader identity changed; existing transfer needs reconciliation",
            )
            if repair:
                await finish_repair(db, repair, "held", attempt.message)
            return
        credentials, endpoint, kind = (
            decrypt_secrets(downloader.encrypted_secrets),
            downloader.base_url,
            downloader.kind,
        )
        already_submitted = attempt.external_may_exist
        attempt.run_token, attempt.lease_until = token, now + timedelta(seconds=LEASE_SECONDS)
        attempt.state = "uncertain" if already_submitted else "preflight"
        tag = attempt_tag(attempt)
        frozen = dict(selection.frozen)
    # Detached selection contains only frozen metadata; no DB connection over I/O.
    try:
        # Measure storage outside transactions; admission commits before client I/O.
        try:
            storage = await capacity.observe_download(frozen)
            async with session_factory()() as db, db.begin():
                attempt, current = await locked(db, identifier)
                if attempt.run_token != token:
                    return
                await capacity.admit(db, attempt, current, storage)
        except capacity.CapacityWait:
            if not already_submitted:
                raise
            # Existing external work must remain observable even during a mount outage.
        if kind == "slskd":
            await observe_soulseek(
                identifier,
                token,
                endpoint=endpoint,
                api_key=credentials["api_key"],
                frozen=frozen,
                already_submitted=already_submitted,
                repair_id=repair_id,
            )
            return
        if kind == "sabnzbd":
            client = SabClient(endpoint, credentials.get("api_key", ""))
        elif kind == "nzbget":
            client = NzbClient(
                endpoint,
                credentials.get("username", ""),
                credentials.get("password", ""),
            )
        elif kind == "transmission":
            client = TransmissionClient(
                endpoint, credentials.get("username", ""), credentials.get("password", "")
            )
        elif kind == "deluge":
            client = DelugeClient(
                endpoint, credentials.get("username", ""), credentials.get("password", "")
            )
        else:
            client = QbitClient(endpoint, credentials["username"], credentials["password"])
        async with asyncio.timeout(NETWORK_SECONDS), client:
            await client.capabilities()
            states = await find(client, selection, tag)
            if not already_submitted:
                if states:
                    raise AdapterError(
                        FailureKind.UNCERTAIN,
                        "An existing transfer conflicts with this new attempt; it was not adopted",
                    )
                storage = await capacity.observe_download(frozen)
                async with session_factory()() as db, db.begin():
                    attempt, current = await locked(db, identifier)
                    if (
                        attempt.run_token != token
                        or attempt.state != "preflight"
                        or attempt.lease_until <= datetime.now(UTC)
                    ):
                        return
                    # Fence preference edits only at the final submission boundary.
                    # Earlier snapshots stay lock-free so list/work locks cannot
                    # invert with this configuration lock. Commit the submission
                    # marker before releasing the fence and performing client I/O.
                    if automatic_dispatch.consent(current):
                        await transaction_lock(db, DEFAULTS_LOCK)
                    await authority(db, current, wanted=True)
                    await capacity.admit(db, attempt, current, storage)
                    await capacity.submitted(db, attempt)
                    # This sticky marker commits before any mutating request.
                    attempt.external_may_exist, attempt.state = True, "submitting"
                    attempt.message = (
                        "Submission recorded; uncertain outcomes will only be reconciled"
                    )
                    (await db.get(Operation, attempt.operation_id)).message = attempt.message
                receipt = await client.submit(
                    content,
                    attempt_tag=tag,
                    save_path=frozen["downloader"]["save_path"],
                    category=frozen["downloader"]["category"],
                )
                async with session_factory()() as db, db.begin():
                    attempt, _ = await locked(db, identifier)
                    if attempt.run_token != token:
                        return
                    attempt.receipt = receipt.model_dump(mode="json")
                states = await find(client, selection, tag)
            if kind == "sabnzbd":
                observed = verify_sab(
                    states,
                    tag=tag,
                    save_path=frozen["downloader"]["save_path"],
                    category=frozen["downloader"]["category"],
                )
            elif kind == "nzbget":
                observed = verify_nzb(
                    states,
                    tag=tag,
                    save_path=frozen["downloader"]["save_path"],
                    category=frozen["downloader"]["category"],
                )
            else:
                from app.adapters.torrent_rpc import verify_untagged

                verify = verify_untagged if kind == "deluge" else verify_association
                observed = verify(
                    states,
                    tag=tag,
                    hashes=hashes(selection),
                    save_path=frozen["downloader"]["save_path"],
                    category=frozen["downloader"]["category"],
                )
            async with session_factory()() as db, db.begin():
                attempt, current = await locked(db, identifier)
                if attempt.run_token != token:
                    return
                repair = await db.get(DownloadRepair, repair_id) if repair_id else None
                if repair:
                    await require_repair_actor(db, repair)
                await authority(
                    db,
                    current,
                    wanted=False,
                    configuration=repair.configuration if repair else None,
                )
                if repair and observed:
                    await finish_repair(
                        db,
                        repair,
                        "applied",
                        "Updated connections verified against the existing transfer; "
                        "no download was added",
                    )
                needs_import = await finish_observation(db, attempt, current, observed)
                if attempt.state == "complete":
                    # Capacity is last in the lock order, after request evaluation
                    # and continuation authority have acquired their domain locks.
                    await capacity.downloaded(db, attempt, needs_import=bool(needs_import))
                db.add(
                    AuditEvent(
                        actor_id=attempt.owner_id,
                        action="acquisition.download.observed",
                        entity_id=attempt.id,
                        detail={"state": attempt.state},
                    )
                )
    except (AdapterError, HTTPException, TimeoutError, capacity.CapacityWait) as error:
        async with session_factory()() as db, db.begin():
            attempt, _ = await locked(db, identifier)
            if attempt.run_token != token:
                return
            transient = isinstance(error, (TimeoutError, capacity.CapacityWait)) or (
                isinstance(error, AdapterError)
                and error.kind
                in {
                    FailureKind.TIMEOUT,
                    FailureKind.UNAVAILABLE,
                    FailureKind.RATE_LIMIT,
                }
            )
            # A refused Soulseek enqueue did not keep a batch, so the slot can be freed.
            if (
                kind == "slskd"
                and isinstance(error, AdapterError)
                and error.kind in {FailureKind.NOT_FOUND, FailureKind.UNSUPPORTED}
            ):
                attempt.external_may_exist = False
                transient = False
            unknown = attempt.external_may_exist and (
                transient or isinstance(error, AdapterError) and error.kind == FailureKind.UNCERTAIN
            )
            message = (
                str(error)
                if isinstance(error, (AdapterError, capacity.CapacityWait))
                else "Download check timed out"
                if transient
                else "Download access, request or saved settings changed"
            )
            attempt.recovery_observation = None
            await record(
                db,
                attempt,
                "uncertain" if unknown else "queued" if transient else "held",
                message[:300],
                poll=transient or unknown,
            )
            if repair_id and not transient:
                repair = await db.get(DownloadRepair, repair_id)
                if repair and repair.state == "pending":
                    await finish_repair(db, repair, "held", message[:300])
