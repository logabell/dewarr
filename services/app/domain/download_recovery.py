"""Failed-transfer recovery. All replacement commands commit before external I/O.

The old transfer and its identity claims are retained forever. A recovery is unique
per member selection; retries resume its saved search/selection, never start another.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    AuditEvent,
    DownloadAttempt,
    DownloadMembership,
    DownloadRecovery,
    DownloadRecoverySettings,
    Operation,
    User,
)
from app.db.session import session_factory
from app.domain import download_memberships, release_blocklist
from app.domain.operations import transaction_lock
from app.jobs.queue import enqueue

KIND = "acquisition.recover-download"
OPEN = {"queued", "searching", "selecting"}


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    stall_hours: float | None = Field(default=24, gt=0, le=8760)
    error_minutes: float = Field(default=5, ge=0, le=10080)
    cleanup: Literal["leave", "pause", "remove"] = "leave"


class RecoveryConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    defaults: RecoveryPolicy = Field(default_factory=RecoveryPolicy)
    sources: dict[str, RecoveryPolicy] = Field(
        default_factory=lambda: {"mam": RecoveryPolicy(stall_hours=None)}, max_length=100
    )
    attempt_cap: int = Field(default=3, ge=1, le=20)
    approve_reports: bool = False


async def configuration(db):
    row = await db.get(DownloadRecoverySettings, 1)
    return RecoveryConfiguration.model_validate(row.configuration if row else {})


def policy_for(config, selection):
    release = selection.frozen["release"]
    source = release["source"]
    indexer = source + ":" + str(release.get("indexer_id", ""))
    return config.sources.get(indexer, config.sources.get(source, config.defaults))


def observe(health, state, policy, now):
    """Only verified observations advance clocks; uncertainty breaks continuity."""
    health = dict(health or {})
    if not state or not state.association_verified:
        return {}, None
    if state.completed:
        return {}, None
    if not policy.enabled:
        return {}, None
    current = getattr(state, "progress", None)
    paused = state.state in {
        "pausedDL",
        "stoppedDL",
        "queuedDL",
        "checkingDL",
        "checkingResumeData",
        # Transmission status codes and Deluge states use the shared observation
        # model but retain their native stopped/queued/checking names.
        "0",
        "1",
        "2",
        "3",
        "5",
        "Paused",
        "Queued",
        "Checking",
        "Allocating",
        "Moving",
    }
    if paused:
        return {}, None
    failure = state.state in {"error", "missingFiles", "failed"}
    if failure:
        health.setdefault("error_since", now.isoformat())
        if now - datetime.fromisoformat(health["error_since"]) >= timedelta(
            minutes=policy.error_minutes
        ):
            return health, "Downloader reported " + state.state
    else:
        health.pop("error_since", None)
    if current is None or current != health.get("progress"):
        health["progress_since"] = now.isoformat()
    health["progress"] = current
    health.setdefault("progress_since", now.isoformat())
    if getattr(state, "seeders", None) == 0:
        health.setdefault("zero_seeders_since", now.isoformat())
    else:
        health.pop("zero_seeders_since", None)
    if policy.stall_hours is not None:
        for field, reason in (
            ("zero_seeders_since", "No seeders"),
            ("progress_since", "No progress"),
        ):
            if field in health and (field != "progress_since" or current is not None):
                if now - datetime.fromisoformat(health[field]) >= timedelta(
                    hours=policy.stall_hours
                ):
                    return health, f"{reason} for {policy.stall_hours:g} hours"
    return health, None


async def event(db, row, name):
    selection = await db.get(AcquisitionSelection, row.selection_id)
    event_type = {
        "stalled": "download.stalled",
        "retried": "download.retried",
        "gave-up": "download.gave_up",
    }.get(name)
    if event_type:
        try:
            from app.notifications.events import record_event
        except ModuleNotFoundError as error:
            if error.name not in {"app.notifications", "app.notifications.events"}:
                raise
            logging.getLogger(__name__).warning(
                "NOR-30 notification delivery unavailable for %s", event_type
            )
        else:
            await record_event(
                db,
                key=f"recovery:{row.id}:{event_type}",
                event_type=event_type,
                owner_id=selection.owner_id,
                subject_id=row.attempt_id,
                title={
                    "stalled": "Download stalled",
                    "retried": "Replacement queued",
                    "gave-up": "Download needs attention",
                }[name],
                message=row.message,
                path="/requests#downloads",
            )
    # Durable transactional event IDs are also the notification deduplication keys.
    db.add(
        AuditEvent(
            actor_id=selection.owner_id,
            action="acquisition.download." + name,
            entity_id=row.id,
            detail={
                "recovery_id": str(row.id),
                "attempt_id": str(row.attempt_id),
                "work_id": selection.frozen["work_id"],
                "reason": row.reason,
                "message": row.message,
            },
        )
    )


async def failed(db, attempt, reason, *, actor_id=None, approval=False):
    """Caller holds download-members + all member work locks (downloads.locked)."""
    config = await configuration(db)
    rows = []
    for selection in await download_memberships.for_attempt(db, attempt.id):
        existing = await db.scalar(
            select(DownloadRecovery).where(DownloadRecovery.selection_id == selection.id)
        )
        if existing:
            rows.append(existing)
            continue
        if actor_id is None and not policy_for(config, selection).enabled:
            continue
        root = (selection.frozen.get("download_recovery") or {}).get("root_selection_id")
        row = DownloadRecovery(
            selection_id=selection.id,
            attempt_id=attempt.id,
            root_selection_id=UUID(root) if root else selection.id,
            state="approval" if approval else "queued",
            reason=reason[:300],
            message="Waiting for replacement approval"
            if approval
            else "Release blocklisted; finding a replacement",
            evidence={
                "cleanup": policy_for(config, selection).cleanup,
                "cleanup_state": "pending",
                "reported": actor_id is not None,
            },
        )
        db.add(row)
        await db.flush()
        await release_blocklist.add(
            db, selection, reason, actor_id or selection.owner_id, automatic=actor_id is None
        )
        # This retires the request scope, never the old transfer or its hash claims.
        selection.state, selection.message = "cancelled", reason[:300]
        reservation = await db.get(AcquisitionReservation, selection.reservation_id)
        reservation.state = "released"
        await event(
            db, row, "stalled" if reason.startswith(("No seeders", "No progress")) else "failed"
        )
        if not approval:
            row.job_id = await enqueue(db, KIND, recovery_id=str(row.id))
        rows.append(row)
    if rows:
        attempt.state, attempt.message = "held", reason[:300]
        attempt.next_check_at = attempt.run_token = attempt.lease_until = None
        operation = await db.get(Operation, attempt.operation_id)
        operation.status, operation.message = "held", attempt.message
    return rows


async def hold(db, row, message):
    row.state, row.message = "held", message[:300]
    selection = await db.get(AcquisitionSelection, row.selection_id)
    target = await db.get(AcquisitionTarget, selection.target_id)
    target.state, target.message = "paused", row.message
    await event(db, row, "gave-up")


async def history(db, selection):
    rows = (
        await db.execute(
            select(AcquisitionSelection, DownloadAttempt)
            .join(DownloadMembership, DownloadMembership.selection_id == AcquisitionSelection.id)
            .join(DownloadAttempt, DownloadAttempt.id == DownloadMembership.attempt_id)
            .where(AcquisitionSelection.target_id == selection.target_id)
            .order_by(DownloadAttempt.created_at, DownloadAttempt.id)
        )
    ).all()
    result = []
    for member, attempt in rows:
        recovery = await db.scalar(
            select(DownloadRecovery).where(DownloadRecovery.selection_id == member.id)
        )
        result.append(
            {
                "attempt_id": str(attempt.id),
                "selection_id": str(member.id),
                "release_title": member.frozen["release"]["title"],
                "state": recovery.state if recovery else attempt.state,
                "reason": recovery.reason if recovery else attempt.message,
            }
        )
    return result


async def frozen_context(db, root_id, rule, profile):
    """Intersect the original contract with current authority, never relax either."""
    from app.domain.acquisition import intersect_rules
    from app.domain.release_profiles import ProfileSnapshot

    root = await db.get(AcquisitionSelection, UUID(str(root_id)))
    merged = intersect_rules(root.frozen["requirements"], rule)
    if not merged:
        raise HTTPException(409, "The original frozen request conflicts with current requirements")
    return root, merged, ProfileSnapshot.model_validate(root.frozen["profile"])


async def run(identifier):
    if get_settings().recovery_mode:
        return
    await cleanup(identifier)
    from app.domain import automatic_selection, book_sources
    from app.domain.acquisition import evaluate
    from app.importing.naming import fingerprint

    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"download-recovery:{identifier}")
        row = await db.get(DownloadRecovery, identifier, populate_existing=True)
        if not row or row.state not in OPEN:
            return
        selection = await db.get(AcquisitionSelection, row.selection_id)
        root = await db.get(AcquisitionSelection, row.root_selection_id)
        await download_memberships.lock(db, [selection])
        user = await db.get(User, selection.owner_id, populate_existing=True)
        intent = await db.get(AcquisitionIntent, selection.intent_id)
        try:
            async with db.begin_nested():
                if not user or not user.active or user.role == "viewer":
                    raise HTTPException(403, "Request access needs attention")
                if not policy_for(
                    await configuration(db), selection
                ).enabled and not row.evidence.get("reported"):
                    raise HTTPException(409, "Automatic recovery is disabled for this source")
                from app.domain.recovery_approvals import require_current

                await require_current(db, "selection", root.id)
                await evaluate(db, user, intent)
                target = await db.get(AcquisitionTarget, selection.target_id)
                if target.state == "satisfied":
                    row.state, row.message = (
                        "complete",
                        "Request is already satisfied by another library copy",
                    )
                    return
                if row.replacement_id:
                    replacement = await db.get(Operation, row.replacement_id)
                    if replacement.status in {"queued", "running"}:
                        await automatic_selection.repair(db, replacement)
                    if (
                        replacement.status == "completed"
                        and replacement.payload.get("selection_id")
                        and not replacement.payload.get("download_id")
                    ):
                        from app.domain.download_attempts import start

                        attempt = await start(
                            db,
                            user,
                            UUID(replacement.payload["selection_id"]),
                            f"recovery-download:{row.id}",
                            automatic=True,
                        )
                        replacement.payload = {
                            **replacement.payload,
                            "download_id": str(attempt.id),
                        }
                    if replacement.status == "completed" and replacement.payload.get("download_id"):
                        row.state, row.message = (
                            "retried",
                            "Replacement download queued with the original constraints",
                        )
                        row.evidence = {
                            **row.evidence,
                            "replacement_attempt_id": replacement.payload["download_id"],
                        }
                        await event(db, row, "retried")
                        return
                    if replacement.status not in automatic_selection.TERMINAL:
                        return
                    # Exhausted saved results may be refreshed once, without resetting the cap.
                    if not row.evidence.get("fresh_search") and not replacement.payload.get(
                        "selection_id"
                    ):
                        row.replacement_id, row.search_id = None, None
                        row.evidence = {**row.evidence, "saved_exhausted": True}
                    else:
                        await hold(db, row, replacement.message)
                        return
                config = await configuration(db)
                attempts = await history(db, selection)
                if len(attempts) >= config.attempt_cap:
                    row.evidence = {**row.evidence, "attempts": attempts}
                    await hold(
                        db,
                        row,
                        f"Gave up after {len(attempts)} attempts: "
                        + "; ".join(
                            f"{i + 1}. {a['release_title']}: {a['reason']}"
                            for i, a in enumerate(attempts)
                        ),
                    )
                    return
                if target.state != "wanted":
                    raise HTTPException(409, "Replacement request is paused or withdrawn")
                if (
                    not row.search_id
                    and not row.evidence.get("fresh_search")
                    and not row.evidence.get("saved_exhausted")
                ):
                    saved = root.command.get("search_id") or (
                        root.frozen.get("automatic_selection") or {}
                    ).get("search_id")
                    search = await db.get(Operation, UUID(saved)) if saved else None
                    if (
                        search
                        and search.status == "completed"
                        and datetime.fromisoformat(search.payload["expires_at"]) > datetime.now(UTC)
                    ):
                        row.search_id = search.id
                if not row.search_id:
                    search = await book_sources.start(
                        db,
                        user,
                        intent.work_id,
                        book_sources.SearchInput(
                            request_id=intent.id, medium=root.frozen["requirements"]["medium"]
                        ),
                        f"recovery-search:{row.id}",
                    )
                    row.search_id, row.state = search.id, "searching"
                    row.evidence = {**row.evidence, "fresh_search": True}
                search = await db.get(Operation, row.search_id)
                if search.status in {"queued", "running"}:
                    return
                if search.status != "completed":
                    await hold(db, row, "Replacement search failed; review source access")
                    return
                proof = root.frozen.get("automatic_selection") or {}
                body = automatic_selection.AutomaticSelectionInput(
                    intent_id=intent.id,
                    slot=root.frozen["slot"],
                    search_id=search.id,
                    downloader_id=root.downloader_id,
                    downloader_generation=root.frozen["downloader"]["generation"],
                    destination_id=root.destination_id,
                    destination_revision=fingerprint(root.frozen["destination"]),
                    download_when_ready=bool(proof.get("dispatch_approval")),
                )
                replacement = await automatic_selection.begin(
                    db,
                    user,
                    body,
                    f"recovery-select:{row.id}:{search.id}",
                    list_authority=proof.get("list_authority"),
                    series_authority=proof.get("series_authority"),
                    recovery_selection_id=root.id,
                )
                row.replacement_id, row.state = replacement.id, "selecting"
                row.message = "Selecting the next eligible release with the original constraints"
        except HTTPException as error:
            await db.refresh(row)
            await hold(db, row, str(error.detail))


async def schedule():
    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        rows = await db.scalars(
            select(DownloadRecovery)
            .where(DownloadRecovery.state.in_(OPEN))
            .order_by(DownloadRecovery.created_at)
            .limit(50)
            .with_for_update(skip_locked=True)
        )
        for row in rows:
            status = await db.scalar(
                text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                {"id": row.job_id},
            )
            if status not in {"todo", "doing"}:
                row.job_id = await enqueue(db, KIND, recovery_id=str(row.id))


async def cleanup(identifier):
    """Reverify identity and route immediately before a non-file-deleting client action."""
    import asyncio

    from app.adapters.contracts import AdapterError
    from app.adapters.qbittorrent import QbitClient, verify_association
    from app.db.models import DownloadCapacity, Integration
    from app.domain import capacity, download_attempts
    from app.importing.naming import fingerprint
    from app.security import decrypt_secrets

    if get_settings().recovery_mode:
        return
    async with session_factory()() as db:
        row = await db.get(DownloadRecovery, identifier)
        if not row or row.state not in OPEN or row.evidence.get("cleanup_state") != "pending":
            return
        attempt = await db.get(DownloadAttempt, row.attempt_id)
        selection = await db.get(AcquisitionSelection, attempt.selection_id)
        client_row = await db.get(Integration, selection.downloader_id)
        action = row.evidence["cleanup"]
        from app.domain.recovery_approvals import require_current

        try:
            await require_current(db, "selection", row.root_selection_id)
        except HTTPException:
            return
        policy = policy_for(await configuration(db), selection)
        if not policy.enabled or policy.cleanup != action:
            action = "leave"
        # Pack member recovery jobs share one physical transfer. Leave is always the
        # conservative choice if member-specific policies disagree.
        siblings = list(
            await db.scalars(
                select(DownloadRecovery).where(DownloadRecovery.attempt_id == attempt.id)
            )
        )
        if any(item.evidence.get("cleanup") != action for item in siblings):
            action = "leave"
        if action == "leave":
            status = "left"
        elif not client_row or client_row.kind != "qbittorrent":
            status = "unsupported"
        elif (
            not client_row.enabled
            or client_row.credential_generation != selection.frozen["downloader"]["generation"]
            or fingerprint({"url": client_row.base_url.rstrip("/")}) != attempt.endpoint_key
        ):
            status = "needs-review"
        else:
            status = None
        if status is None:
            credentials = decrypt_secrets(client_row.encrypted_secrets)
            endpoint = client_row.base_url
    if status is None:
        try:
            async with (
                asyncio.timeout(60),
                QbitClient(endpoint, credentials["username"], credentials["password"]) as client,
            ):
                observed = verify_association(
                    await download_attempts.find(
                        client, selection, download_attempts.attempt_tag(attempt)
                    ),
                    tag=download_attempts.attempt_tag(attempt),
                    hashes=download_attempts.hashes(selection),
                    save_path=selection.frozen["downloader"]["save_path"],
                    category=selection.frozen["downloader"]["category"],
                )
                if observed:
                    accepted = await client.cleanup_transfer(
                        observed.external_id, remove=action == "remove"
                    )
                    status = (
                        "removed"
                        if accepted and action == "remove"
                        else "paused"
                        if accepted
                        else "needs-review"
                    )
                else:
                    status = "absent"
        except (AdapterError, TimeoutError):
            status = "needs-review"
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, f"download-members:{attempt.id}")
        for row in await db.scalars(
            select(DownloadRecovery).where(DownloadRecovery.attempt_id == attempt.id)
        ):
            row.evidence = {**row.evidence, "cleanup_state": status}
        if status in {"removed", "paused", "absent"}:
            await transaction_lock(db, capacity.LOCK)
            reservation = await db.get(DownloadCapacity, attempt.id)
            if reservation:
                # Files remain on disk, so their storage reservations remain too.
                reservation.slot_active = False


async def reject_inspected(attempt_id):
    from app.db.models import AutomaticImport, DownloadInspection
    from app.domain.download_attempts import locked
    from app.domain.download_reviews import has_imports

    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        attempt, selection = await locked(db, attempt_id)
        if not attempt or attempt.state != "complete":
            return
        automatic = await db.scalar(
            select(AutomaticImport).where(AutomaticImport.attempt_id == attempt.id)
        )
        if (
            not automatic
            or not automatic.evidence.get("release_rejection")
            or automatic.state != "held"
        ):
            return
        inspection = await db.get(DownloadInspection, automatic.inspection_id)
        if not inspection or inspection.state != "ready" or await has_imports(db, inspection.id):
            return
        await failed(
            db, attempt, "Inspected release rejected: wrong book, language, or incomplete content"
        )


def replacement_folders(plan, selections):
    """Give each replacement publication its own deterministic, non-overwriting folder."""
    from app.importing.naming import component

    for item in plan.items:
        selection = next(
            (
                s
                for s in selections
                if s.frozen.get("download_recovery")
                and UUID(s.frozen["origin_work_id"]) == item.work_id
                and s.frozen["requirements"]["medium"] == item.medium
            ),
            None,
        )
        if selection is None or not item.folder or item.state != "ready":
            continue
        original = item.folder
        parent, _, leaf = original.rpartition("/")
        leaf = component(leaf, 110) + " [replacement " + selection.id.hex + "]"
        item.folder = (parent + "/" if parent else "") + leaf
        if len(item.folder.encode()) > 1000:
            raise HTTPException(409, "Replacement library path exceeds the supported length")
        for file in item.files:
            if not file.destination.startswith(original + "/"):
                raise HTTPException(409, "Replacement publication path needs review")
            file.destination = item.folder + file.destination[len(original) :]
        item.warnings.append(
            "Replacement is added alongside the existing copy; library files are preserved"
        )


async def replacement_exclusions(db, inspection_id):
    from app.db.models import ReportedDownloadAsset

    selections = await db.scalars(
        select(AcquisitionSelection)
        .join(DownloadMembership, DownloadMembership.selection_id == AcquisitionSelection.id)
        .join(DownloadAttempt, DownloadAttempt.id == DownloadMembership.attempt_id)
        .where(
            DownloadAttempt.inspection_id == inspection_id,
            AcquisitionSelection.state == "committed",
        )
    )
    excluded = set()
    for selection in selections:
        root_id = (selection.frozen.get("download_recovery") or {}).get("root_selection_id")
        if root_id:
            excluded.update(
                await db.scalars(
                    select(ReportedDownloadAsset.asset_id)
                    .join(
                        DownloadRecovery, DownloadRecovery.id == ReportedDownloadAsset.recovery_id
                    )
                    .where(
                        DownloadRecovery.root_selection_id == UUID(root_id),
                        ReportedDownloadAsset.owner_id == selection.owner_id,
                    )
                )
            )
    return excluded
