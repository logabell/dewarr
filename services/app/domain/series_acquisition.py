"""Acquire a finite accepted series set through the ordinary acquisition pipeline."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_, select, text

from app.config import get_settings
from app.db.models import AcquisitionIntent, AcquisitionReason, AcquisitionTarget, Operation, User
from app.db.session import session_factory
from app.domain import acquisition, automatic_routes, list_automation
from app.domain.acquisition import RequestSpec
from app.domain.release_profiles import ProfileSnapshot, refresh_profile, same_profile
from app.domain.work_graph import acquisition_lock, canonical_work, graph_lock
from app.importing.naming import fingerprint
from app.jobs.queue import enqueue

KIND = "series.acquire"
DISCOVERY_REVISION = 1


async def configuration(db, user, specification, profile, routes):
    routes, route_origins = await automatic_routes.inherit(db, user, specification, profile, routes)
    libraries, approvals = await automatic_routes.resolve(
        db, user, specification, routes.downloader_id, routes.downloader_generation, routes.routes
    )
    spec = RequestSpec.model_validate({**specification.model_dump(mode="json"), **libraries})
    profile = profile.model_copy(
        update={
            "scope_origins": {
                **profile.scope_origins,
                **route_origins,
                **dict.fromkeys(libraries, "Series import route"),
            }
        }
    )
    return {
        "specification": spec.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json"),
        **routes.model_dump(mode="json"),
        "approvals": approvals,
    }


async def validate_configuration(db, user, config):
    profile = ProfileSnapshot.model_validate(config["profile"])
    if not same_profile(await refresh_profile(db, user.id, profile), profile):
        raise HTTPException(409, "Series acquisition preferences changed; create a new preview")
    current = await configuration(
        db,
        user,
        RequestSpec.model_validate(config["specification"]),
        profile,
        automatic_routes.AutomaticRoutes.model_validate(
            {key: config[key] for key in ("downloader_id", "downloader_generation", "routes")}
        ),
    )
    if current != config:
        raise HTTPException(409, "Series acquisition routes changed; create a new preview")


def scope_revision(parent):
    return fingerprint(
        {
            "records": parent.payload["records"],
            "configuration": parent.payload["automatic_configuration"],
            "specification": parent.payload["effective_specification"],
            **(
                {"pack_origin": parent.payload["pack_origin"]}
                if parent.payload.get("pack_origin")
                else {}
            ),
            **(
                {"list_origin": parent.payload["list_origin"]}
                if parent.payload.get("list_origin")
                else {}
            ),
        }
    )


def proof(controller):
    return {
        "operation_id": controller.payload["parent_id"],
        "acquisition_id": str(controller.id),
        "scope_revision": controller.payload["scope_revision"],
        **(
            {"pack_origin": controller.payload["pack_origin"]}
            if controller.payload.get("pack_origin")
            else {}
        ),
        **(
            {"list_origin": controller.payload["list_origin"]}
            if controller.payload.get("list_origin")
            else {}
        ),
    }


async def lock_authority(db, authority):
    if not authority:
        return
    from app.domain.list_series import lock_origin

    await lock_origin(db, authority.get("list_origin"))
    for key in ("operation_id", "acquisition_id"):
        await db.get(
            Operation, UUID(authority[key]), with_for_update={"read": True}, populate_existing=True
        )


async def require_authority(db, owner_id, authority, *, intent_id):
    if not authority:
        return
    parent = await db.get(Operation, UUID(authority["operation_id"]), populate_existing=True)
    controller = await db.get(Operation, UUID(authority["acquisition_id"]), populate_existing=True)
    user = await db.get(User, owner_id, populate_existing=True)
    automatic_routes.permitted(user)
    if (
        not parent
        or parent.kind != "series.requests"
        or parent.owner_id != owner_id
        or parent.status != "completed"
        or not parent.payload.get("accepted_at")
        or not controller
        or controller.kind != KIND
        or controller.owner_id != owner_id
        or parent.payload.get("acquisition_id") != str(controller.id)
        or not controller.payload["enabled"]
        or proof(controller) != authority
        or scope_revision(parent) != authority["scope_revision"]
        or not any(r["request_id"] == str(intent_id) for r in parent.payload.get("receipt", []))
        or not await db.scalar(
            select(AcquisitionReason.id)
            .where(
                AcquisitionReason.intent_id == intent_id,
                AcquisitionReason.kind == "series",
                AcquisitionReason.reference == str(parent.id),
                AcquisitionReason.active.is_(True),
            )
            .limit(1)
        )
    ):
        raise HTTPException(
            409, "Series acquisition authorization changed; review the saved request"
        )
    from app.domain.list_series import require_origin

    await require_origin(db, owner_id, parent.payload.get("list_origin"))
    from app.domain.pack_expansion import require_origin as require_pack

    await require_pack(db, owner_id, parent.payload.get("pack_origin"))
    intent = await db.get(AcquisitionIntent, intent_id)
    work = await canonical_work(db, intent.work_id)
    receipt = next(r for r in parent.payload["receipt"] if r["request_id"] == str(intent_id))
    record = next(r for r in parent.payload["records"] if r["work_id"] == receipt["work_id"])
    if (str(work.id), work.title, work.authors) != (
        record["work_id"],
        record["title"],
        record["authors"],
    ):
        raise HTTPException(409, "Accepted series book identity changed; create a new preview")
    await validate_configuration(db, user, controller.payload["configuration"])


async def initialize(db, parent):
    if not parent.payload.get("automatic_configuration") or parent.payload.get("acquisition_id"):
        return
    controller = Operation(
        owner_id=parent.owner_id,
        kind=KIND,
        idempotency_key=f"series-acquisition:{parent.id}",
        message="Waiting to acquire the reviewed series books",
        payload={
            "parent_id": str(parent.id),
            **(
                {"pack_origin": deepcopy(parent.payload["pack_origin"])}
                if parent.payload.get("pack_origin")
                else {}
            ),
            **(
                {"list_origin": deepcopy(parent.payload["list_origin"])}
                if parent.payload.get("list_origin")
                else {}
            ),
            "scope_revision": scope_revision(parent),
            "configuration": parent.payload["automatic_configuration"],
            "enabled": True,
            "revision": 1,
            "discovery_revision": DISCOVERY_REVISION,
            "next_at": datetime.now(UTC).isoformat(),
            "books": {
                r["work_id"]: {
                    **r,
                    "progress": {},
                    "state": "wanted",
                    "message": "Waiting to search",
                    "next_at": datetime.now(UTC).isoformat(),
                }
                for r in parent.payload["receipt"]
            },
        },
    )
    db.add(controller)
    await db.flush()
    parent.payload = {**parent.payload, "acquisition_id": str(controller.id)}
    controller.job_id = await enqueue(db, KIND, operation_id=str(controller.id))


async def cancel(db, parent):
    if not parent.payload.get("acquisition_id"):
        return
    row = await db.get(Operation, UUID(parent.payload["acquisition_id"]), with_for_update=True)
    row.payload = {**row.payload, "enabled": False, "next_at": None}
    row.status, row.message = (
        "cancelled",
        "Series acquisition cancelled; existing files are preserved",
    )


async def retry(db, user, parent):
    if not parent.payload.get("acquisition_id") or parent.status != "completed":
        raise HTTPException(409, "No active accepted series acquisition to retry")
    from app.domain.list_series import require_origin

    await require_origin(db, user.id, parent.payload.get("list_origin"))
    from app.domain.pack_expansion import require_origin as require_pack

    await require_pack(db, user.id, parent.payload.get("pack_origin"))
    row = await db.get(Operation, UUID(parent.payload["acquisition_id"]), with_for_update=True)
    from app.domain.operations import require_live_command

    await require_live_command(db, row)
    from app.domain.recovery_approvals import require_current

    await require_current(db, "operation", row.id)
    if not row.payload["enabled"]:
        raise HTTPException(409, "This series acquisition has finished or was cancelled")
    await validate_configuration(db, user, row.payload["configuration"])
    status = await job_status(db, row)
    if status in {"todo", "doing"}:
        return
    payload = deepcopy(row.payload)
    payload.pop("upstream_hold", None)
    payload["revision"] += 1
    payload["next_at"] = datetime.now(UTC).isoformat()
    for book in payload["books"].values():
        if book["state"] != "available":
            book["next_at"] = payload["next_at"]
            book["progress"]["resume_attempts"] = True
    row.payload = payload
    row.status, row.message = "queued", "Rechecking this accepted series acquisition"
    row.job_id = await enqueue(db, KIND, operation_id=str(row.id))


async def job_status(db, row):
    return await db.scalar(
        text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
        {"id": row.job_id},
    )


async def schedule():
    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        rows = list(
            await db.scalars(
                select(Operation)
                .where(
                    Operation.kind == KIND,
                    Operation.status.in_(["queued", "running"]),
                    Operation.payload["enabled"].as_boolean().is_(True),
                    or_(
                        Operation.payload["next_at"].astext <= datetime.now(UTC).isoformat(),
                        Operation.payload["discovery_revision"].astext.is_distinct_from(
                            str(DISCOVERY_REVISION)
                        ),
                    ),
                )
                .order_by(Operation.created_at, Operation.id)
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            status = await job_status(db, row)
            if status in {"todo", "doing"}:
                continue
            if status in {"failed", "aborted"}:
                row.status, row.message = (
                    "held",
                    "Series worker stopped; retry the saved acquisition",
                )
                continue
            row.job_id = await enqueue(db, KIND, operation_id=str(row.id))
            row.payload = {
                **row.payload,
                "next_at": list_automation.next_tick(datetime.now(UTC)).isoformat(),
            }


async def run(identifier):
    if get_settings().recovery_mode:
        return
    async with session_factory()() as db, db.begin():
        row = await db.get(Operation, identifier)
        if not row or row.kind != KIND or row.payload.get("recovery_retirement"):
            return
        from app.domain.list_series import lock_origin, require_origin

        await lock_origin(db, row.payload.get("list_origin"))
        from app.domain.pack_expansion import (
            require_origin as require_pack,
        )

        parent = await db.get(
            Operation, UUID(row.payload["parent_id"]), with_for_update={"read": True}
        )
        await db.refresh(row, with_for_update=True)
        if not row.payload["enabled"] or row.status in {"held", "completed", "cancelled"}:
            return
        payload = deepcopy(row.payload)
        user = await db.get(User, row.owner_id)
        try:
            await require_origin(db, row.owner_id, parent.payload.get("list_origin"))
            await require_pack(db, row.owner_id, parent.payload.get("pack_origin"))
        except HTTPException as error:
            row.payload = {**payload, "upstream_hold": True}
            row.status, row.message = "held", str(error.detail)
            return
        try:
            automatic_routes.permitted(user)
            if parent.status != "completed" or scope_revision(parent) != payload["scope_revision"]:
                raise HTTPException(409, "The accepted series scope changed; review its request")
            await validate_configuration(db, user, payload["configuration"])
        except HTTPException as error:
            row.status, row.message = "held", str(error.detail)
            return
        config = payload["configuration"]
        policy = SimpleNamespace(
            id=row.id, generation=1, revision=payload["revision"], configuration=config
        )
        now = datetime.now(UTC)
        if payload.get("discovery_revision") != DISCOVERY_REVISION:
            # Upgrade only active, already-approved requests that were waiting
            # after the old single search pass. Never revive a held transfer or
            # broaden children of an explicitly selected collection.
            if not payload.get("pack_origin"):
                for saved in payload["books"].values():
                    for slot in ("ebook", "audio", "either"):
                        progress = saved["progress"].get(slot, {})
                        if (
                            progress.get("next_at")
                            and progress.get("round")
                            and not progress.get("search_id")
                            and not progress.get("selection_id")
                        ):
                            progress["refined"] = True
                            progress.pop("next_at", None)
                            saved["next_at"] = now.isoformat()
            payload["discovery_revision"] = DISCOVERY_REVISION
        # Keep accepted identities stable while checking consent and taking work
        # locks. Every series controller visits the same work order.
        await graph_lock(db)
        for saved in sorted(payload["books"].values(), key=lambda book: book["work_id"]):
            if not saved.get("next_at") or datetime.fromisoformat(saved["next_at"]) > now:
                continue
            original = deepcopy(saved)
            try:
                async with db.begin_nested():
                    intent = await db.get(AcquisitionIntent, UUID(saved["request_id"]))
                    await require_authority(db, user.id, proof(row), intent_id=intent.id)
                    await acquisition_lock(db, intent.work_id)
                    await acquisition.evaluate(db, user, intent)
                    await db.flush()
                    book = SimpleNamespace(
                        id=intent.id,
                        intent_id=intent.id,
                        work_id=intent.work_id,
                        progress=saved["progress"],
                    )
                    targets = list(
                        await db.scalars(
                            select(AcquisitionTarget)
                            .where(AcquisitionTarget.intent_id == intent.id)
                            .order_by(AcquisitionTarget.slot)
                        )
                    )
                    outcomes = []
                    for target in targets:
                        progress = book.progress.setdefault(target.slot, {})
                        if book.progress.get("resume_attempts"):
                            progress["resume_attempt"] = True
                        outcomes.append(
                            await list_automation.advance_target(
                                db,
                                user,
                                policy,
                                book,
                                target,
                                progress,
                                now,
                                series_authority=proof(row),
                            )
                        )
                    book.progress.pop("resume_attempts", None)
                    state, message, _ = min(
                        outcomes,
                        key=lambda v: {
                            "held": 0,
                            "searching": 1,
                            "selecting": 2,
                            "pending": 3,
                            "wanted": 4,
                            "available": 5,
                        }[v[0]],
                    )
                    saved.update(state=state, message=message)
                    due = [v[2] for v in outcomes if v[2]]
                    saved["next_at"] = min(due).isoformat() if due else None
            except HTTPException as error:
                saved.clear()
                saved.update(original)
                saved.update(state="held", message=str(error.detail), next_at=None)
        waiting = [
            b["next_at"]
            for b in payload["books"].values()
            if b["next_at"] and b["state"] != "available"
        ]
        all_available = all(b["state"] == "available" for b in payload["books"].values())
        payload["next_at"] = min(waiting) if waiting else None
        if all_available:
            payload["enabled"] = False
        row.payload = payload
        row.status = "completed" if all_available else "running" if waiting else "held"
        row.message = (
            "All reviewed books are available in the requested media"
            if all_available
            else "Acquiring the reviewed series books"
            if waiting
            else "Some series books need attention; review their progress"
        )
