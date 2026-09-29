"""Finite series requests preserve accepted scope and independent acquisition reasons."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select, text

from app.config import get_settings
from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AuditEvent,
    CatalogSeries,
    Operation,
    SeriesMembership,
    User,
    Work,
)
from app.db.session import session_factory
from app.domain.acquisition import (
    RequestReason,
    RequestSpec,
    assess,
    evaluate,
    submit,
    validate_request,
)
from app.domain.automatic_routes import AutomaticRoutes
from app.domain.list_requests import BatchInput, identity, pending_targets
from app.domain.operations import require_live_command, transaction_lock
from app.domain.permissions import (
    apply_approval_wait,
    auto_approves,
    series_batch_message,
    waiting_for_approval,
)
from app.domain.request_preferences import resolve
from app.domain.series_projection import full_book
from app.domain.visibility import visible_work
from app.domain.work_graph import acquisition_lock, canonical_map, canonical_work, graph_lock
from app.jobs.queue import enqueue

KIND = "series.requests"


class SeriesRequestInput(BatchInput):
    scope: Literal["selected", "complete_series"] = "selected"
    confirm_main_membership: bool = False
    scope_review_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    expected_generation: int = Field(ge=1)
    automatic: AutomaticRoutes | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def reviewed_scope(self):
        if self.scope_review_id and self.scope != "complete_series":
            raise ValueError("A saved main-book review applies to the complete reviewed set")
        if self.scope == "complete_series" and not (
            self.confirm_main_membership or self.scope_review_id
        ):
            raise ValueError("Confirm the selected main books before requesting this series")
        return self


async def context(db, user_id, external_id):
    from app.domain.series_scopes import lock

    await lock(db, user_id, external_id)
    # Catalog publication takes this before graph/member writes. Join that order
    # before retaining a catalog row lock, rather than deadlocking with a refresh
    # and a queued exclusive identity correction.
    await transaction_lock(db, f"series-catalog:{user_id}:{external_id}")
    # The source token is not needed to request books from an already observed catalog.
    user = await db.get(User, user_id, with_for_update={"read": True}, populate_existing=True)
    if not user or not user.active or user.role == "viewer":
        raise HTTPException(403, "Your account can no longer request books")
    row = await db.scalar(
        select(CatalogSeries)
        .where(
            CatalogSeries.owner_id == user_id,
            CatalogSeries.provider == "hardcover",
            CatalogSeries.external_id == external_id,
        )
        .with_for_update(read=True)
    )
    if not row or not row.fetched_at:
        raise HTTPException(409, "Load and verify this series catalog before requesting books")
    return row, user


async def membership(db, user, series, body):
    await graph_lock(db)
    mapping = canonical_map()
    pairs = (
        await db.execute(
            select(SeriesMembership, Work)
            .join(mapping, mapping.c.origin_id == SeriesMembership.work_id)
            .join(Work, Work.id == mapping.c.work_id)
            .where(
                SeriesMembership.series_id == series.id,
                SeriesMembership.present.is_(True),
                visible_work(user),
            )
            .order_by(Work.id, SeriesMembership.external_id)
        )
    ).all()
    selected = set(body.work_ids)
    groups = {}
    for entry, work in pairs:
        groups.setdefault(work.id, (work, []))[1].append(entry.snapshot)
    positions = {}
    for work_id, (_, members) in groups.items():
        for member in members:
            if member["position"] is not None and full_book(member):
                positions.setdefault(member["position"], set()).add(work_id)
    if not selected <= set(groups):
        raise HTTPException(409, "Series membership or book identity changed; select books again")
    records, omitted = [], []
    for work_id, (work, members) in groups.items():
        warnings = []
        if any(len(positions.get(member["position"], set())) > 1 for member in members):
            warnings.append("Multiple works at this position; verify membership")
        normal = [r for r in members if full_book(r)]
        published = any(
            r["release_date"] and r["release_date"] <= datetime.now(UTC).date().isoformat()
            for r in normal
        )
        if not normal:
            warnings.append("Compilation, partial or merged record")
        if not published:
            warnings.append("Publication or complete-book evidence needs review")
        if work_id not in selected:
            omitted.append({**identity(work), "warnings": warnings, "reason": "Not selected"})
            continue
        if body.scope == "complete_series" and (not normal or not published):
            raise HTTPException(
                409, "Complete series requires published full books; review the selection"
            )
        positions_for_work = [
            m["position"] for m in (normal or members) if m["position"] is not None
        ]
        records.append(
            {
                **identity(work),
                "warnings": warnings,
                "members": members,
                "position": min(positions_for_work, key=Decimal) if positions_for_work else None,
            }
        )
    return records, omitted


async def preview(db, user, external_id, body, key):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Series requests are paused for recovery")
    await transaction_lock(db, f"operation:{user.id}:{key}")
    command = {
        **body.model_dump(mode="json"),
        "external_id": external_id,
        "work_ids": sorted(map(str, body.work_ids)),
    }
    existing = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if existing:
        if existing.kind != KIND or existing.payload.get("command") != command:
            raise HTTPException(409, "This preview key was used for different options")
        return existing
    series, user = await context(db, user.id, external_id)
    if series.generation != body.expected_generation:
        raise HTTPException(409, "Series catalog changed; reload and review your selection")
    spec, profile = await resolve(
        db, user, body.specification, RequestReason(), body.release_preferences
    )
    automatic = None
    if body.automatic:
        from app.domain.series_acquisition import configuration

        automatic = await configuration(db, user, spec, profile, body.automatic)
        spec = RequestSpec.model_validate(automatic["specification"])
        profile = profile.model_copy(
            update={"scope_origins": automatic["profile"]["scope_origins"]}
        )
    records, omitted = await membership(db, user, series, body)
    scope_review = None
    if body.scope_review_id:
        from app.domain.series_scopes import require_current

        scope_review = await require_current(db, user, series, body.scope_review_id, body.work_ids)
    for record in records:
        await validate_request(db, user, UUID(record["work_id"]), spec)
    operation = Operation(
        owner_id=user.id,
        kind=KIND,
        idempotency_key=key,
        status="preview",
        message=(
            "Review this finite set of books before sending them for approval"
            if not automatic and not auto_approves(user, spec)
            else "Review this finite set of books and requested media"
        ),
        payload={
            "command": command,
            "records": records,
            "omitted": omitted,
            "series": {
                "id": str(series.id),
                "name": series.name,
                "external_id": external_id,
                "generation": series.generation,
                "fetched_at": series.fetched_at.isoformat(),
            },
            "effective_specification": spec.model_dump(mode="json"),
            "release_policy": profile.model_dump(mode="json"),
            "automatic_configuration": automatic,
            "main_membership": "user-confirmed"
            if body.scope == "complete_series"
            else "not-asserted",
            **({"scope_review": scope_review} if scope_review else {}),
            "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
        },
    )
    db.add(operation)
    await db.flush()
    return operation


async def owned(db, user, external_id, operation_id):
    from app.domain.list_series import lock_origin, origin

    initial = await db.get(Operation, operation_id)
    if initial and initial.owner_id == user.id and initial.kind == KIND:
        await lock_origin(db, origin(initial))
    _, user = await context(db, user.id, external_id)
    operation = await db.scalar(
        select(Operation)
        .where(
            Operation.id == operation_id,
            Operation.owner_id == user.id,
            Operation.kind == KIND,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not operation or operation.payload["command"]["external_id"] != external_id:
        raise HTTPException(404, "Series request not found")
    if operation.status in {"queued", "running"}:
        state = await db.scalar(
            text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
            {"id": operation.job_id},
        )
        if state not in {"todo", "doing"}:
            operation.status, operation.message = (
                "failed",
                "The worker stopped; retry this saved request",
            )
    return operation


async def validate_identities(db, user, operation):
    await graph_lock(db)
    spec = RequestSpec.model_validate(operation.payload["effective_specification"])
    works = []
    for record in operation.payload["records"]:
        work = await validate_request(db, user, UUID(record["work_id"]), spec)
        if identity(work) != {k: record[k] for k in ("work_id", "title", "authors")}:
            raise HTTPException(409, "Book identity changed; create a new series preview")
        works.append(work)
    return works, spec


async def start(db, user, operation):
    await require_live_command(db, operation)
    if get_settings().recovery_mode:
        raise HTTPException(409, "Series requests are paused for recovery")
    from app.domain.list_series import origin, require_origin

    await require_origin(db, user.id, origin(operation))
    from app.domain.pack_expansion import require_origin as require_pack

    await require_pack(db, user.id, operation.payload.get("pack_origin"))
    if operation.status in {"queued", "running", "completed"}:
        return
    if operation.status == "cancelled":
        raise HTTPException(409, "This series request was cancelled; create a new preview")
    if not operation.payload.get("accepted_at"):
        if datetime.fromisoformat(operation.payload["expires_at"]) <= datetime.now(UTC):
            raise HTTPException(409, "Series preview expired; create a new preview")
        row = await db.get(CatalogSeries, UUID(operation.payload["series"]["id"]))
        if row.generation != operation.payload["series"]["generation"]:
            raise HTTPException(409, "Series catalog changed; create a new preview")
        body = SeriesRequestInput.model_validate(
            {k: v for k, v in operation.payload["command"].items() if k != "external_id"}
        )
        if body.scope_review_id:
            from app.domain.series_scopes import require_current

            proof = await require_current(db, user, row, body.scope_review_id, body.work_ids)
            if proof != operation.payload.get("scope_review"):
                raise HTTPException(409, "Main-book review changed; create a new preview")
        await resolve(
            db,
            user,
            body.specification,
            RequestReason(),
            body.release_preferences,
            expected=operation.payload["release_policy"]["effective_revision"],
        )
    await validate_identities(db, user, operation)
    if operation.payload.get("automatic_configuration"):
        from app.domain.series_acquisition import validate_configuration

        await validate_configuration(db, user, operation.payload["automatic_configuration"])
    operation.payload = {
        **operation.payload,
        **({"upstream_hold": False} if operation.payload.get("list_origin") else {}),
        "accepted_at": operation.payload.get("accepted_at") or datetime.now(UTC).isoformat(),
    }
    operation.status, operation.message = "queued", "Waiting to save the selected series requests"
    operation.job_id = await enqueue(db, KIND, operation_id=str(operation.id))


async def run(operation_id):
    if get_settings().recovery_mode:
        raise RuntimeError("Series requests are paused for recovery")
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id)
        if not operation or operation.kind != KIND or operation.payload.get("recovery_retirement"):
            return
        from app.domain.list_series import lock_origin, origin, require_origin

        await lock_origin(db, origin(operation))
        from app.domain.pack_expansion import (
            require_origin as require_pack,
        )

        # Reserve child command locks before parent/identity/work locks, matching submit.
        for work_id in operation.payload["command"]["work_ids"]:
            await transaction_lock(
                db, f"operation:{operation.owner_id}:series-request:{operation.id}:{work_id}"
            )
        try:
            _, user = await context(
                db, operation.owner_id, operation.payload["command"]["external_id"]
            )
        except HTTPException:
            await db.refresh(operation, with_for_update=True)
            if operation.status in {"queued", "running"}:
                operation.status, operation.message = (
                    "failed",
                    "Series or account access needs attention",
                )
            return
        await db.refresh(operation, with_for_update=True)
        if operation.status not in {"queued", "running"}:
            return
        try:
            await require_origin(db, operation.owner_id, origin(operation))
            await require_pack(db, operation.owner_id, operation.payload.get("pack_origin"))
        except HTTPException as error:
            operation.payload = {**operation.payload, "upstream_hold": True}
            operation.status, operation.message = "failed", str(error.detail)
            return
        try:
            works, spec = await validate_identities(db, user, operation)
        except HTTPException as error:
            operation.status, operation.message = "failed", str(error.detail)
            return
        for work in works:
            await acquisition_lock(db, work.id)
        receipts = []
        for work in works:
            intent, _ = await submit(
                db,
                user,
                work.id,
                spec,
                RequestReason(),
                f"series-request:{operation.id}:{work.id}",
                frozen_preferences=operation.payload["release_policy"],
                series_reference=operation.id,
                hold_for_approval=not operation.payload.get("automatic_configuration"),
            )
            receipts.append({"work_id": str(work.id), "request_id": str(intent.id)})
        operation.payload = {**operation.payload, "receipt": receipts}
        waiting = 0
        for receipt in receipts:
            if await waiting_for_approval(db, UUID(receipt["request_id"])):
                waiting += 1
        operation.status, operation.message = (
            "completed",
            series_batch_message(len(receipts), waiting),
        )
        if operation.payload.get("automatic_configuration"):
            from app.domain.series_acquisition import initialize

            await initialize(db, operation)
            operation.message = f"Saved {len(receipts)} reviewed books for automatic acquisition"
        db.add(
            AuditEvent(actor_id=user.id, action="series.requests.completed", entity_id=operation.id)
        )


async def cancel(db, user, operation):
    if operation.status == "cancelled":
        return
    from app.domain.series_acquisition import cancel as cancel_acquisition

    await cancel_acquisition(db, operation)
    await graph_lock(db)
    intents = list(
        await db.scalars(
            select(AcquisitionIntent)
            .join(AcquisitionReason)
            .where(
                AcquisitionIntent.owner_id == user.id,
                AcquisitionReason.kind == "series",
                AcquisitionReason.reference == str(operation.id),
            )
            .order_by(AcquisitionIntent.work_id)
        )
    )
    roots = sorted({(await canonical_work(db, i.work_id)).id for i in intents}, key=str)
    for work_id in roots:
        await acquisition_lock(db, work_id)
    reasons = list(
        await db.scalars(
            select(AcquisitionReason).where(
                AcquisitionReason.kind == "series", AcquisitionReason.reference == str(operation.id)
            )
        )
    )
    for reason in reasons:
        reason.active = False
    await db.flush()
    for intent in intents:
        await evaluate(db, user, intent)
    operation.status, operation.message = (
        "cancelled",
        "Series request cancelled; other request reasons and existing files are preserved",
    )
    db.add(AuditEvent(actor_id=user.id, action="series.requests.cancelled", entity_id=operation.id))


async def status_records(db, user, operation):
    spec = RequestSpec.model_validate(operation.payload["effective_specification"])
    records = []
    for record in operation.payload["records"]:
        try:
            work = await validate_request(db, user, UUID(record["work_id"]), spec)
            outcomes = await pending_targets(
                db, user, work.id, spec, await assess(db, user, work.id, spec)
            )
            receipt = next(
                (
                    r
                    for r in operation.payload.get("receipt", [])
                    if r["work_id"] == record["work_id"]
                ),
                None,
            )
            withdrawn = receipt and not await db.scalar(
                select(AcquisitionReason.id).where(
                    AcquisitionReason.intent_id == UUID(receipt["request_id"]),
                    AcquisitionReason.kind == "series",
                    AcquisitionReason.reference == str(operation.id),
                    AcquisitionReason.active.is_(True),
                )
            )
            if operation.status == "cancelled" or withdrawn:
                for target in outcomes:
                    if target["state"] != "satisfied":
                        target.update(
                            state="cancelled", message="This series request was cancelled"
                        )
            elif receipt and await waiting_for_approval(db, UUID(receipt["request_id"])):
                apply_approval_wait(outcomes)
            records.append(
                {
                    **record,
                    "_origin_work_id": record["work_id"],
                    "work_id": str(work.id),
                    "title": work.title,
                    "targets": outcomes,
                    "issue": None,
                }
            )
        except HTTPException:
            records.append(
                {
                    **record,
                    "_origin_work_id": record["work_id"],
                    "title": "Book needs review",
                    "authors": [],
                    "warnings": [],
                    "targets": [],
                    "issue": "Book identity or library access needs attention",
                }
            )
    return sorted(
        records,
        key=lambda r: (
            r.get("position") is None,
            Decimal(r["position"]) if r.get("position") is not None else Decimal(0),
            r["title"].casefold(),
            r["work_id"],
        ),
    )
