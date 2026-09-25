"""One-click acquisition using frozen reader preferences and the existing selector."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from app.adapters.source_releases import SOURCE_NAMES
from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReservation,
    AcquisitionTarget,
    MonitoredRelease,
    Operation,
    SourceConnection,
    User,
)
from app.db.session import session_factory
from app.domain import (
    acquisition,
    automatic_routes,
    automatic_selection,
    book_sources,
    source_strategy,
)
from app.domain.downloader_defaults import protocol_default
from app.domain.downloaders import transfer_connection
from app.domain.operations import transaction_lock
from app.domain.release_dates import release_facts, search_allowed
from app.domain.release_monitor import sync_monitor
from app.domain.release_profiles import ProfileSnapshot
from app.domain.work_graph import canonical_work
from app.jobs.queue import enqueue

KIND = "acquisition.quick-add"


def _idle_outcome(targets, pending, held):
    """A wanted request is not already available. Only a satisfied request is."""
    if pending:
        return "queued", "Searching for your preferred releases"
    if held or any(target.state == "wanted" for target in targets):
        message = "; ".join(target.message for target in held if target.message)
        return "held", message or "This request is still open, so no download was started"
    return "completed", "Already available; no duplicate download started"


async def _search_targets(db, targets):
    pending = []
    held = [target for target in targets if target.state not in {"wanted", "satisfied"}]
    for target in targets:
        reservation = (
            await db.get(AcquisitionReservation, target.reservation_id)
            if target.reservation_id
            else None
        )
        if target.state == "wanted" and reservation and reservation.state == "planned":
            pending.append(target)
    return pending, held


async def begin(db, user, work_id, options, key, *, dispatch=False):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Quick add is paused for recovery")
    from app.domain.permissions import (
        MANAGE_REQUESTS,
        approval_dispatch,
        auto_approves,
        download_authorization,
        has,
    )

    if dispatch:
        if not (has(user, MANAGE_REQUESTS) and auto_approves(user, options)):
            raise HTTPException(403, "You cannot download this request")
    elif not auto_approves(user, options):
        raise HTTPException(403, "This account can request books, but a download needs approval")
    token = approval_dispatch.set(dispatch)
    grant = download_authorization.set(options)
    try:
        return await _begin(db, user, work_id, options, key, dispatch=dispatch)
    finally:
        download_authorization.reset(grant)
        approval_dispatch.reset(token)


async def _begin(db, user, work_id, options, key, *, dispatch):
    automatic_routes.permitted(user)
    command = {"work_id": str(work_id), "specification": options.model_dump(mode="json")}
    await transaction_lock(db, f"operation:{user.id}:{key}")
    previous = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if previous:
        if previous.kind != KIND or previous.payload["command"] != command:
            raise HTTPException(409, "This quick-add key was already used for another command")
        return previous
    intent, _ = await acquisition.submit(
        db, user, work_id, options, acquisition.RequestReason(), f"quick-request:{key}"
    )
    # submit holds the work lock, serializing repeated clicks even with different keys.
    active = await db.scalar(
        select(Operation)
        .where(
            Operation.owner_id == user.id,
            Operation.kind == KIND,
            Operation.payload["intent_id"].astext == str(intent.id),
            Operation.status.in_(["queued", "running"]),
        )
        .limit(1)
    )
    if active:
        return active
    await acquisition.evaluate(db, user, intent)
    await db.flush()
    targets = list(
        await db.scalars(
            select(AcquisitionTarget)
            .where(AcquisitionTarget.intent_id == intent.id)
            .order_by(AcquisitionTarget.slot)
        )
    )
    pending, held = await _search_targets(db, targets)
    payload = {
        "command": command,
        "intent_id": str(intent.id),
        "slots": {},
        "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        **({"approval_dispatch": True} if dispatch else {}),
    }
    status, message = _idle_outcome(targets, pending, held)
    operation = Operation(
        owner_id=user.id,
        kind=KIND,
        idempotency_key=key,
        status=status,
        payload=deepcopy(payload),
        message=message,
    )
    db.add(operation)
    await db.flush()
    if pending:
        work = await canonical_work(db, intent.work_id)
        day, _, coming = release_facts(work.metadata_fields)
        if not search_allowed(day, datetime.now(UTC).date(), coming_soon=coming):
            operation.status = "held"
            operation.message = (
                f"Waiting until {day.isoformat()}; source search starts on release day"
                if day
                else "Waiting for a release date; source search starts once the day is known"
            )
            payload["waiting_for_release"] = day.isoformat() if day else None
            operation.payload = deepcopy(payload)
            await sync_monitor(db, user, work, operation, command["specification"])
            return operation
        await start_search(db, user, operation, intent, pending, held, payload)
        await sync_monitor(db, user, work, operation, command["specification"])
        return operation
    work = await canonical_work(db, intent.work_id)
    if await db.scalar(
        select(MonitoredRelease).where(
            MonitoredRelease.owner_id == user.id, MonitoredRelease.work_id == work.id
        )
    ):
        await sync_monitor(db, user, work, operation, command["specification"])
    return operation


async def start_search(db, user, operation, intent, pending, held, payload):
    spec = acquisition.RequestSpec.model_validate(intent.specification)
    profile = ProfileSnapshot.model_validate(intent.release_policy)
    # A copy already in the library needs no download route.
    route_spec = spec
    if spec.mode == "both" and len(pending) == 1:
        route_spec = spec.model_copy(update={"mode": pending[0].slot})
    routes, _ = await automatic_routes.inherit(
        db, user, route_spec, profile, automatic_routes.AutomaticRoutes()
    )
    await automatic_routes.resolve(
        db, user, route_spec, routes.downloader_id, routes.downloader_generation, routes.routes
    )
    connected = {
        row.key
        for row in await db.scalars(
            select(SourceConnection).where(SourceConnection.enabled.is_(True))
        )
        if row.key in SOURCE_NAMES
    }
    order = source_strategy.search_order(profile.preferences.source_order, connected)
    plan = {
        "strategy": profile.preferences.source_strategy,
        "fallback": profile.preferences.source_fallback,
        "order": order,
        "index": 0,
        "tried": [],
    }
    only = [order[0]] if plan["strategy"] == "priority" and order else None
    payload["routes"] = routes.model_dump(mode="json")
    payload["source_plan"] = plan
    operation.message = (
        f"Searching {SOURCE_NAMES[order[0]]}" if only else "Searching connected sources"
    )
    search = await book_sources.start(
        db,
        user,
        intent.work_id,
        book_sources.SearchInput(
            request_id=intent.id, medium=spec.mode if spec.mode in {"audio", "ebook"} else "all"
        ),
        f"quick-search:{operation.id}:{order[0] if only else 'all'}",
        only_sources=only,
    )
    payload["search_id"] = str(search.id)
    payload["slots"] = {
        **{target.slot: {} for target in pending},
        **{
            target.slot: {"done": True, "failed": True, "message": target.message}
            for target in held
        },
    }
    operation.payload = deepcopy(payload)
    operation.job_id = await enqueue(db, KIND, operation_id=str(operation.id))
    return operation


async def resume_search(db, user, operation):
    """Start the search that was parked until the release day, on the same request."""
    from app.domain.permissions import approval_dispatch, download_authorization

    payload = deepcopy(operation.payload)
    if "waiting_for_release" not in payload or operation.status not in {"held", "queued"}:
        return operation
    try:
        intent_id = UUID(payload["intent_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(409, "The parked request is no longer available") from error
    intent = await db.get(AcquisitionIntent, intent_id)
    if intent is None:
        raise HTTPException(409, "The parked request is no longer available")
    targets = list(
        await db.scalars(
            select(AcquisitionTarget)
            .where(AcquisitionTarget.intent_id == intent.id)
            .order_by(AcquisitionTarget.slot)
        )
    )
    pending, held = await _search_targets(db, targets)
    payload.pop("waiting_for_release", None)
    # The parked request was created before release day. The quick-add window starts now.
    payload["expires_at"] = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    grant_spec = acquisition.RequestOptions.model_validate(payload["command"]["specification"])
    token = approval_dispatch.set(bool(payload.get("approval_dispatch")))
    grant = download_authorization.set(grant_spec)
    try:
        if pending:
            await start_search(db, user, operation, intent, pending, held, payload)
            operation.status = "queued"
        else:
            operation.status, operation.message = _idle_outcome(targets, pending, held)
            operation.payload = payload
        work = await canonical_work(db, intent.work_id)
        await sync_monitor(db, user, work, operation, payload["command"]["specification"])
    finally:
        download_authorization.reset(grant)
        approval_dispatch.reset(token)
    return operation


async def run(identifier):
    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, identifier, with_for_update=True)
        if (
            not operation
            or operation.kind != KIND
            or operation.status not in {"queued", "running"}
            or operation.payload.get("recovery_retirement")
        ):
            return
        payload = deepcopy(operation.payload)
        from app.domain.permissions import approval_dispatch, download_authorization

        grant_spec = acquisition.RequestOptions.model_validate(payload["command"]["specification"])
        token = approval_dispatch.set(bool(payload.get("approval_dispatch")))
        grant = download_authorization.set(grant_spec)
        try:
            user = await db.get(User, operation.owner_id)
            automatic_routes.permitted(user)
            if datetime.fromisoformat(payload["expires_at"]) <= datetime.now(UTC):
                raise HTTPException(409, "Quick add timed out; check Downloads before retrying")
            search, changed = await book_sources.refresh_search(
                db, UUID(payload["search_id"]), user.id
            )
            if changed:
                raise HTTPException(409, "Book details changed; search sources again")
            if search.status == "completed":
                intent = await db.get(AcquisitionIntent, UUID(payload["intent_id"]))
                spec = acquisition.RequestSpec.model_validate(intent.specification)
                routes = automatic_routes.AutomaticRoutes.model_validate(payload["routes"])
                for slot, progress in payload["slots"].items():
                    if progress.get("done"):
                        continue
                    try:
                        async with db.begin_nested():
                            if progress.get("operation_id"):
                                child = await db.get(Operation, UUID(progress["operation_id"]))
                                await automatic_selection.repair(db, child)
                            else:
                                # Either requests use the preferred medium first, as configured.
                                medium = spec.preferred_medium if slot == "either" else slot
                                plan = payload.get("source_plan") or {}
                                source_key = "all"
                                if plan.get("strategy") == "priority" and plan.get("order"):
                                    source_key = plan["order"][plan.get("index", 0)]
                                child = await automatic_selection.begin(
                                    db,
                                    user,
                                    automatic_selection.AutomaticSelectionInput(
                                        intent_id=intent.id,
                                        slot=slot,
                                        search_id=search.id,
                                        **automatic_routes.selection_clients(routes, medium),
                                        download_when_ready=True,
                                    ),
                                    f"quick-select:{operation.id}:{slot}:{source_key}",
                                )
                            progress["operation_id"] = str(child.id)
                            progress["message"] = child.message
                            if child.status in automatic_selection.TERMINAL:
                                plan = payload.setdefault(
                                    "source_plan",
                                    {
                                        "strategy": "rank_all",
                                        "fallback": False,
                                        "order": [],
                                        "index": 0,
                                        "tried": [],
                                    },
                                )
                                found = child.status == "completed"
                                decision = source_strategy.outcome(
                                    plan.get("strategy", "rank_all"),
                                    bool(plan.get("fallback")),
                                    int(plan.get("index") or 0),
                                    len(plan.get("order") or []),
                                    found=found,
                                )
                                if decision == "next":
                                    progress["wants_next"] = True
                                    progress["message"] = child.message
                                else:
                                    progress.pop("wants_next", None)
                                    progress["done"] = True
                                    progress["failed"] = not found
                                    if not found:
                                        tried = list(plan.get("tried") or [])
                                        order = plan.get("order") or []
                                        index = int(plan.get("index") or 0)
                                        if order and index < len(order):
                                            current = order[index]
                                            tried.append(
                                                {
                                                    "name": SOURCE_NAMES.get(current, current),
                                                    "message": child.message,
                                                }
                                            )
                                        progress["message"] = source_strategy.review_message(
                                            tried, None if tried else child.message
                                        )
                    except automatic_selection.AlreadyAvailable:
                        progress.update(done=True, message="Already in your library")
                    except HTTPException as error:
                        progress.update(done=True, failed=True, message=str(error.detail))
                if source_strategy.ready_for_next_source(payload["slots"]):
                    plan = payload.setdefault(
                        "source_plan",
                        {
                            "strategy": "rank_all",
                            "fallback": False,
                            "order": [],
                            "index": 0,
                            "tried": [],
                        },
                    )
                    order = plan.get("order") or []
                    index = int(plan.get("index") or 0)
                    current = order[index]
                    missed = next(
                        (
                            progress.get("message")
                            for progress in payload["slots"].values()
                            if not progress.get("done") and progress.get("wants_next")
                        ),
                        None,
                    )
                    plan["tried"].append(
                        {
                            "name": SOURCE_NAMES.get(current, current),
                            "message": missed,
                        }
                    )
                    plan["index"] = index + 1
                    nxt = order[plan["index"]]
                    search = await book_sources.start(
                        db,
                        user,
                        intent.work_id,
                        book_sources.SearchInput(
                            request_id=intent.id,
                            medium=spec.mode if spec.mode in {"audio", "ebook"} else "all",
                        ),
                        f"quick-search:{operation.id}:{nxt}",
                        only_sources=[nxt],
                    )
                    payload["search_id"] = str(search.id)
                    for progress in payload["slots"].values():
                        if progress.get("done") or not progress.get("wants_next"):
                            continue
                        progress.pop("operation_id", None)
                        progress.pop("wants_next", None)
                        progress["message"] = f"Searching {SOURCE_NAMES.get(nxt, nxt)}"
                complete = all(p.get("done") for p in payload["slots"].values())
                operation.status = (
                    (
                        "held"
                        if any(p.get("failed") for p in payload["slots"].values())
                        else "completed"
                    )
                    if complete
                    else "running"
                )
                operation.message = " · ".join(
                    f"{'Audiobook' if slot == 'audio' else slot.capitalize()}: "
                    f"{p.get('message', 'Selecting release')}"
                    for slot, p in payload["slots"].items()
                )
            else:
                plan = payload.get("source_plan") or {}
                current = None
                if plan.get("strategy") == "priority" and plan.get("order"):
                    current = plan["order"][int(plan.get("index") or 0)]
                operation.status, operation.message = (
                    "running",
                    f"Searching {SOURCE_NAMES[current]}" if current else search.message,
                )
        except HTTPException as error:
            operation.status, operation.message = "held", str(error.detail)
        finally:
            download_authorization.reset(grant)
            approval_dispatch.reset(token)
        operation.payload = payload
        if operation.status in {"queued", "running"}:
            operation.job_id = await enqueue(
                db, KIND, schedule_in={"seconds": 3}, operation_id=str(identifier)
            )


async def repair(db, operation):
    if operation.status not in {"queued", "running"}:
        return
    state = await db.scalar(
        text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
        {"id": operation.job_id},
    )
    if state not in {"todo", "doing"}:
        operation.status = "held"
        operation.message = "Quick add stopped. Check Downloads before retrying."


async def selected_release(db, user, search_id, result_id, key, *, use_wedge=False):
    """Download only the clicked result using the reader's configured route."""
    from app.adapters.source_releases import release_value
    from app.db.models import SourceResult
    from app.domain.permissions import auto_approves, download_authorization

    await transaction_lock(db, f"selected-release:{user.id}:{key}")
    previous = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if previous:
        command = previous.payload.get("command", {})
        if (
            previous.kind != automatic_selection.KIND
            or command.get("search_id") != str(search_id)
            or command.get("result_id") != str(result_id)
            or bool(command.get("use_wedge")) != bool(use_wedge)
        ):
            raise HTTPException(409, "This download key was already used for another release")
        return previous
    search, changed = await book_sources.checked(db, search_id, user.id)
    row = await db.get(SourceResult, result_id)
    if not row or row.owner_id != user.id or row.operation_id != search_id:
        raise HTTPException(404, "Source release not found")
    if changed:
        raise HTTPException(409, "Book details changed; refresh sources")
    release = release_value(row.source_key, row.release_snapshot)
    if release.medium not in {"ebook", "audio"}:
        raise HTTPException(409, "The release medium is unknown; inspect this release first")
    granted = acquisition.RequestOptions(mode=release.medium)
    if not auto_approves(user, granted):
        raise HTTPException(403, "This account can request books, but a download needs approval")
    grant = download_authorization.set(granted)
    try:
        return await _selected_release(
            db, user, search, search_id, result_id, key, release, use_wedge=use_wedge
        )
    finally:
        download_authorization.reset(grant)


async def _selected_release(
    db, user, search, search_id, result_id, key, release, *, use_wedge=False
):
    automatic_routes.permitted(user)
    bound = search.payload.get("command", {}).get("request_id")
    if bound:
        intent = await db.get(AcquisitionIntent, UUID(bound))
        if not intent or intent.owner_id != user.id:
            raise HTTPException(404, "Request not found")
    else:
        intent, _ = await acquisition.submit(
            db,
            user,
            UUID(search.payload["work"]["id"]),
            acquisition.RequestOptions(mode=release.medium),
            acquisition.RequestReason(),
            f"release-request:{key}",
        )
    spec = acquisition.RequestSpec.model_validate(intent.specification)
    slot = "either" if spec.mode == "either" else release.medium
    if spec.mode not in {"both", "either", release.medium}:
        raise HTTPException(409, "This release does not match the request's medium")
    route_spec = spec.model_copy(update={"mode": release.medium})
    profile = ProfileSnapshot.model_validate(intent.release_policy)
    protocol = automatic_selection.wanted_protocol(release.protocol)
    client_id = await protocol_default(db, profile.preferences, protocol)
    if not client_id:
        label = {"torrent": "torrent", "nzb": "Usenet", "soulseek": "Soulseek"}[protocol]
        raise HTTPException(
            422, f"Configure a default {label} client in Settings → Download clients."
        )
    downloader = await transfer_connection(db, client_id)
    routes, _ = await automatic_routes.inherit(
        db,
        user,
        route_spec,
        profile,
        automatic_routes.AutomaticRoutes(
            downloader_id=downloader.id, downloader_generation=downloader.credential_generation
        ),
        include_fallback=False,
    )
    await automatic_routes.resolve(
        db, user, route_spec, routes.downloader_id, routes.downloader_generation, routes.routes
    )
    return await automatic_selection.begin(
        db,
        user,
        automatic_selection.AutomaticSelectionInput(
            intent_id=intent.id,
            slot=slot,
            search_id=search_id,
            result_id=result_id,
            download_when_ready=True,
            use_wedge=use_wedge,
            **automatic_routes.selection_clients(routes, release.medium),
        ),
        key,
    )
