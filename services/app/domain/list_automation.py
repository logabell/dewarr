"""List orchestration queues the shared search/selection pipeline; it never submits torrents."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    ListAcquisitionBook,
    ListAcquisitionPolicy,
    Operation,
)
from app.db.session import session_factory
from app.domain import (
    acquisition,
    automatic_selection,
    book_sources,
    list_monitoring,
    list_policies,
)
from app.domain.acquisition import RequestReason, RequestSpec
from app.domain.list_requests import owner_context
from app.domain.work_graph import acquisition_lock
from app.jobs.queue import enqueue

KIND = "lists.acquire"


def next_tick(now):
    # Cron runs on minute boundaries. Carrying this worker's subsecond offset
    # forward would skip the next tick whenever that tick happens a little earlier.
    return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)


def proof(policy, book):
    return {
        "policy_id": str(policy.id),
        "list_id": str(policy.list_id),
        "generation": policy.generation,
        "book_id": str(book.id),
    }


def configuration_input(config):
    routes = config.get("route_options", config)
    return list_policies.ListPolicyInput(
        mode=config["mode"],
        specification=config.get(
            "scope_options",
            {
                **config["specification"],
                "download_constraints": config.get("request_constraints"),
            },
        ),
        profile_id=config["profile"]["id"],
        profile_generation=config["profile"]["generation"],
        profile_effective_revision=config["profile"].get("base_effective_revision")
        or config["profile"].get("effective_revision"),
        preference_overrides=config.get(
            "preference_overrides", config["profile"].get("list_overrides") or {}
        ),
        downloader_id=routes["downloader_id"],
        downloader_generation=routes["downloader_generation"],
        routes=routes["routes"],
    )


def retry_at(round_number, now):
    return now + (
        timedelta(hours=6) if round_number == 1 else timedelta(days=1 if round_number <= 8 else 7)
    )


async def schedule():
    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        now = datetime.now(UTC)
        policies = list(
            await db.scalars(
                select(ListAcquisitionPolicy)
                .where(
                    ListAcquisitionPolicy.active.is_(True),
                    ListAcquisitionPolicy.list_id.is_not(None),
                    ListAcquisitionPolicy.configuration["mode"].astext == "automatic",
                    ListAcquisitionPolicy.next_check_at <= now,
                )
                .order_by(ListAcquisitionPolicy.next_check_at, ListAcquisitionPolicy.id)
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        )
        for policy in policies:
            previous = await db.get(Operation, policy.operation_id) if policy.operation_id else None
            if previous and previous.status in {"queued", "running"}:
                state = await db.scalar(
                    text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
                    {"id": previous.job_id},
                )
                if state in {"todo", "doing"}:
                    continue
                previous.status, previous.message = (
                    "failed",
                    "Scheduler recovered a stopped policy worker",
                )
            operation = Operation(
                owner_id=policy.owner_id,
                kind=KIND,
                idempotency_key=f"policy-tick:{policy.id}:{now.isoformat()}",
                payload={"policy_id": str(policy.id)},
                message="Checking monitored list books",
            )
            db.add(operation)
            await db.flush()
            operation.job_id = await enqueue(db, KIND, operation_id=str(operation.id))
            policy.operation_id = operation.id
            policy.next_check_at = next_tick(now)


async def backoff_or_alternate(db, user, spec, book, target, progress, medium, now):
    tried = set(progress.get("tried", [])) | {medium}
    if target.slot == "either" and len(tried) == 1:
        other = "audio" if medium == "ebook" else "ebook"
        intent = await db.get(AcquisitionIntent, book.intent_id)
        reservation = await acquisition.reserve(db, user, intent, spec, "either", only_medium=other)
        target.reservation_id = reservation.id
        await db.flush()
        await acquisition.release_unused(db, intent.work_id)
        progress.update(
            search_id=None, selection_id=None, tried=list(tried), next_at=now.isoformat()
        )
    else:
        next_at = retry_at(progress.get("round", 1), now)
        progress.update(search_id=None, selection_id=None, tried=[], next_at=next_at.isoformat())


async def advance_target(db, user, policy, book, target, progress, now, *, series_authority=None):
    cycle = f"{book.id}:{policy.generation}:{book.progress.get('activation', 1)}:{target.slot}"
    authority = series_authority or proof(policy, book)
    authority_key = "series_authority" if series_authority else "list_authority"
    if series_authority:
        cycle = f"series:{policy.id}:{cycle}"
    if target.state == "satisfied":
        return "available", target.message, now + timedelta(hours=24)
    if target.state != "wanted":
        return "held", target.message, None
    reservation = await db.get(AcquisitionReservation, target.reservation_id)
    config = policy.configuration
    spec = RequestSpec.model_validate(config["specification"])
    medium = reservation.requirements["medium"]
    if reservation.state in {"selected", "committed"}:
        # A pause may hold an unsubmitted attempt. Resume only that exact policy's
        # original authorized attempt, with its existing identity/capacity ledger.
        selected = await db.scalar(
            select(AcquisitionSelection).where(
                AcquisitionSelection.reservation_id == reservation.id,
                AcquisitionSelection.state == "committed",
            )
        )
        if selected:
            from app.domain.download_memberships import attempt_for

            attempt = await attempt_for(db, selected.id)
            if (
                attempt
                and attempt.state == "held"
                and not attempt.external_may_exist
                and (selected.frozen.get("automatic_selection") or {}).get(authority_key)
                == authority
                and (
                    progress.get("resume_attempt")
                    or policy.revision > progress.get("policy_revision", policy.revision)
                )
            ):
                attempt.state, attempt.message = (
                    "queued",
                    "Acquisition resumed; rechecking the existing download attempt",
                )
                attempt.next_check_at = now
                op = await db.get(Operation, attempt.operation_id)
                op.status, op.message = "queued", attempt.message
                op.job_id = await enqueue(db, "acquisition.download", attempt_id=str(attempt.id))
                progress["policy_revision"] = policy.revision
            elif attempt and attempt.state == "held":
                return "held", "This download needs attention in Activity", None
        progress.pop("resume_attempt", None)
        return (
            "pending",
            "A compatible acquisition is already in progress",
            next_tick(now),
        )
    if progress.get("next_at") and datetime.fromisoformat(progress["next_at"]) > now:
        return (
            "wanted",
            "No eligible release yet; another search is scheduled",
            datetime.fromisoformat(progress["next_at"]),
        )
    if progress.get("selection_id"):
        operation = await db.get(Operation, UUID(progress["selection_id"]))
        await automatic_selection.repair(db, operation)
        if operation.status in {"queued", "running"}:
            return "selecting", operation.message, next_tick(now)
        if operation.status == "completed" and operation.payload.get("download_id"):
            return (
                "pending",
                "Download queued; awaiting library confirmation",
                next_tick(now),
            )
        if policy.revision > progress.get("policy_revision", policy.revision):
            progress.update(search_id=None, selection_id=None, next_at=now.isoformat())
            progress["policy_revision"] = policy.revision
            return "wanted", "List resumed; a fresh search is scheduled", now
        await backoff_or_alternate(db, user, spec, book, target, progress, medium, now)
        return "wanted", operation.message, datetime.fromisoformat(progress["next_at"])
    if progress.get("search_id"):
        search = await db.get(Operation, UUID(progress["search_id"]))
        if datetime.fromisoformat(search.payload["expires_at"]) <= now:
            await backoff_or_alternate(db, user, spec, book, target, progress, medium, now)
            return (
                "wanted",
                "Search expired; another search is scheduled",
                datetime.fromisoformat(progress["next_at"]),
            )
        if search.status != "completed":
            return "searching", search.message, next_tick(now)
        existing = await db.scalar(
            select(Operation)
            .where(
                Operation.owner_id == user.id,
                Operation.kind == automatic_selection.KIND,
                Operation.payload["command"]["intent_id"].astext == str(book.intent_id),
                Operation.payload["command"]["slot"].astext == target.slot,
                Operation.status.in_(["queued", "running"]),
            )
            .order_by(Operation.created_at.desc())
            .limit(1)
        )
        if existing:
            await automatic_selection.repair(db, existing)
            if existing.status in {"queued", "running"}:
                return (
                    "pending",
                    "A compatible selection is already running",
                    next_tick(now),
                )
        from app.domain.automatic_routes import AutomaticRoutes, selection_clients

        routes = AutomaticRoutes.model_validate(
            {
                key: config[key]
                for key in (
                    "downloader_id",
                    "downloader_generation",
                    "routes",
                    "alternate_downloader_id",
                    "alternate_downloader_generation",
                    "alternate_routes",
                )
                if key in config
            }
        )
        command = automatic_selection.AutomaticSelectionInput(
            intent_id=book.intent_id,
            slot=target.slot,
            search_id=search.id,
            download_when_ready=True,
            **selection_clients(routes, medium),
        )
        try:
            operation = await automatic_selection.begin(
                db,
                user,
                command,
                f"list-select:{cycle}:{progress['round']}",
                **{authority_key: authority},
            )
        except HTTPException as error:
            if error.status_code == 409 and (
                "already running" in str(error.detail)
                or "already has an acquisition" in str(error.detail)
            ):
                return (
                    "pending",
                    "A compatible selection is already running",
                    next_tick(now),
                )
            raise
        progress["selection_id"] = str(operation.id)
        progress["policy_revision"] = policy.revision
        return (
            "selecting",
            "Selecting a release using saved acquisition preferences",
            next_tick(now),
        )
    progress["round"] = progress.get("round", 0) + 1
    search = await book_sources.start(
        db,
        user,
        book.work_id,
        book_sources.SearchInput(
            medium=medium,
            request_id=book.intent_id,
            profile_id=config["profile"]["id"],
            profile_generation=config["profile"]["generation"],
            profile_effective_revision=config["profile"].get("base_effective_revision")
            or config["profile"].get("effective_revision"),
        ),
        f"list-search:{cycle}:{progress['round']}",
        pack_origin=(series_authority or {}).get("pack_origin"),
    )
    progress["search_id"] = str(search.id)
    progress.pop("next_at", None)
    return (
        "searching",
        search.message
        if (series_authority or {}).get("pack_origin")
        else "Searching connected sources for missing media",
        next_tick(now),
    )


async def advance_book(db, user, policy, book, now):
    from app.domain.follows import source, wait_for_release

    if await wait_for_release(db, user, policy, book, now):
        return
    from app.domain import list_series
    from app.domain.release_profiles import ProfileSnapshot

    if book.progress.get("series_request_id"):
        await list_series.advance(db, user, policy, book, None, now)
        return
    scope = ProfileSnapshot.model_validate(
        policy.configuration["profile"]
    ).preferences.effective_series_scope
    planned = None
    if scope == "complete_series" and not book.progress.get("single_book_scope"):
        planned = book.progress.get("accepted_series_plan") or await list_series.plan(
            db, user, book.work_id
        )
        if planned["state"] not in {"ready", "single"}:
            book.state = "wanted" if planned["state"] == "busy" else "held"
            book.message, book.next_check_at = planned["message"], next_tick(now)
            book.progress = {**book.progress, "series_scope_issue": planned}
            return
        book.progress = {
            key: value for key, value in book.progress.items() if key != "series_scope_issue"
        }
        if planned["state"] == "single":
            book.progress = {**book.progress, "single_book_scope": planned}
    await acquisition_lock(db, book.work_id)
    spec = RequestSpec.model_validate(policy.configuration["specification"])
    if not book.intent_id:
        intent, _ = await acquisition.submit(
            db,
            user,
            book.work_id,
            spec,
            RequestReason(list_id=policy.list_id),
            f"list-request:{book.id}:{policy.generation}:{book.progress.get('activation', 1)}",
            policy_reference=list_policies.reason_reference(policy),
            frozen_preferences=policy.configuration["profile"],
            hold_for_approval=bool(await source(db, policy.list_id)),
        )
        book.intent_id = intent.id
        await db.flush()
    else:
        await list_policies.require_authority(
            db, user.id, proof(policy, book), intent_id=book.intent_id
        )
        intent = await db.get(AcquisitionIntent, book.intent_id)
        await acquisition.evaluate(db, user, intent)
        await db.flush()
    if planned and planned["state"] == "ready":
        await list_series.advance(db, user, policy, book, planned, now)
        return
    outcomes = []
    progress = deepcopy(book.progress)
    targets = list(
        await db.scalars(
            select(AcquisitionTarget)
            .where(AcquisitionTarget.intent_id == book.intent_id)
            .order_by(AcquisitionTarget.slot)
        )
    )
    for target in targets:
        state = progress.setdefault(target.slot, {})
        if progress.get("resume_attempts"):
            state["resume_attempt"] = True
        outcomes.append(await advance_target(db, user, policy, book, target, state, now))
    progress.pop("resume_attempts", None)
    book.progress = progress
    priority = {
        "held": 0,
        "searching": 1,
        "selecting": 2,
        "pending": 3,
        "wanted": 4,
        "available": 5,
    }
    state, message, _ = min(outcomes, key=lambda item: priority[item[0]])
    book.state, book.message = state, message
    due = [item[2] for item in outcomes if item[2]]
    book.next_check_at = min(due) if due else None


async def run(identifier):
    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, identifier)
        if (
            not operation
            or operation.kind != KIND
            or operation.status == "completed"
            or operation.payload.get("recovery_retirement")
        ):
            return
        policy = await db.get(ListAcquisitionPolicy, UUID(operation.payload["policy_id"]))
        if not policy or not policy.list_id:
            await db.refresh(operation, with_for_update=True)
            if operation.payload.get("recovery_retirement"):
                return
            operation.status, operation.message = "completed", "The list was removed"
            return
        try:
            _, user = await owner_context(db, policy.owner_id, policy.list_id)
            await db.refresh(policy, with_for_update=True)
            # The command can be retired while this worker waits for list/policy locks.
            await db.refresh(operation, with_for_update=True)
            if operation.payload.get("recovery_retirement") or operation.status == "completed":
                return
            if not policy.active or policy.configuration["mode"] != "automatic":
                operation.status, operation.message = "completed", "List acquisition is paused"
                return
            actual = await list_policies.configuration(
                db, user, policy.list_id, configuration_input(policy.configuration)
            )
            if actual != policy.configuration:
                raise HTTPException(
                    409, "Policy settings or route approval changed; preview activation again"
                )
        except HTTPException as error:
            await db.refresh(operation, with_for_update=True)
            if operation.payload.get("recovery_retirement"):
                return
            operation.status, operation.message = "completed", str(error.detail)
            policy.message = str(error.detail)
            return
        now = datetime.now(UTC)
        records = await list_policies.members(db, user, policy.list_id)
        leaders = await list_monitoring.reconcile(db, policy, records, now)
        due = list(
            await db.scalars(
                select(ListAcquisitionBook)
                .where(
                    ListAcquisitionBook.policy_id == policy.id,
                    ListAcquisitionBook.generation == policy.generation,
                    ListAcquisitionBook.id.in_(leaders),
                    ListAcquisitionBook.next_check_at <= now,
                )
                .order_by(ListAcquisitionBook.next_check_at, ListAcquisitionBook.id)
                .limit(25)
            )
        )
        for book in due:
            try:
                async with db.begin_nested():
                    await advance_book(db, user, policy, book, now)
            except HTTPException as error:
                await db.refresh(book)
                book.state, book.message, book.next_check_at = "held", str(error.detail), None
        operation.status, operation.message = "completed", f"Checked {len(due)} monitored books"
        policy.message = "Monitoring list additions and requested media"
