"""Atomic local list edits with replay receipts and canonical membership semantics."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Text, cast, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app.db.models import AuditEvent, ListEntry, ListObservation, ListSubscription, Operation, Work
from app.domain.acquisition import withdraw_list_reasons
from app.domain.list_requests import owner_context
from app.domain.operations import transaction_lock
from app.domain.visibility import visible_work
from app.domain.work_graph import canonical_map, graph_lock


class CurationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add", "remove"]
    work_ids: list[UUID] = Field(min_length=1, max_length=100)
    expected_revision: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.work_ids)) != len(self.work_ids):
            raise ValueError("Select each book only once")
        return self


class CurationReceipt(BaseModel):
    id: UUID
    action: Literal["add", "remove"]
    changed: int
    selected: int


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def settings_revision(item):
    return digest([item.id, item.name, item.description, item.shared])


async def content_revision(db, list_id):
    # Resolve this list's origins (including roots outside the list), not the
    # entire catalog for every shelf request.
    mapping = canonical_map(select(ListEntry.work_id).where(ListEntry.list_id == list_id))
    # Aggregate in PostgreSQL so paging does not hydrate every membership in Python.
    row = (
        cast(ListEntry.id, Text)
        + literal(":")
        + cast(ListEntry.work_id, Text)
        + literal(":")
        + cast(mapping.c.work_id, Text)
        + literal(":")
        + cast(ListEntry.position, Text)
    )
    fingerprint = await db.scalar(
        select(
            func.md5(
                func.coalesce(
                    func.string_agg(row, aggregate_order_by(literal("|"), ListEntry.id)), ""
                )
            )
        )
        .select_from(ListEntry)
        .join(mapping, mapping.c.origin_id == ListEntry.work_id)
        .where(ListEntry.list_id == list_id)
    )
    return digest([list_id, "membership-v2", fingerprint])


async def check_revision(db, list_id, expected):
    if expected is not None and expected != await content_revision(db, list_id):
        raise HTTPException(
            409, "List membership or order changed. Refresh the list and review your selection."
        )


async def curate(db, user, list_id, body, key, *, suppress_writeback=False):
    _, user = await owner_context(db, user.id, list_id)
    await transaction_lock(db, f"operation:{user.id}:{key}")
    command = {"list_id": str(list_id), **body.model_dump(mode="json")}
    operation = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if operation:
        if operation.kind != "lists.curate" or operation.payload["command"] != command:
            raise HTTPException(409, "This command key was already used for a different edit")
        return CurationReceipt(id=operation.id, **operation.payload["receipt"])
    await graph_lock(db)
    await check_revision(db, list_id, body.expected_revision)
    mapping = canonical_map()
    resolved = dict(
        (
            await db.execute(
                select(mapping.c.origin_id, Work.id)
                .join(Work, Work.id == mapping.c.work_id)
                .where(mapping.c.origin_id.in_(body.work_ids), visible_work(user))
            )
        ).all()
    )
    if set(resolved) != set(body.work_ids):
        raise HTTPException(404, "One or more selected books are no longer accessible")
    roots = list(dict.fromkeys(resolved[key] for key in body.work_ids))
    rows = (
        await db.execute(
            select(ListEntry, mapping.c.work_id)
            .join(mapping, mapping.c.origin_id == ListEntry.work_id)
            .where(ListEntry.list_id == list_id)
        )
    ).all()
    existing = {root: entry for entry, root in rows}
    changed_roots = set()
    changed = 0
    if body.action == "add":
        position = max((entry.position for entry, _ in rows), default=0)
        for root in roots:
            if root in existing:
                existing[root].locally_added = True
                continue
            position += 1
            db.add(ListEntry(list_id=list_id, work_id=root, position=position))
            changed_roots.add(root)
            changed += 1
    else:
        origins = select(mapping.c.origin_id).where(mapping.c.work_id.in_(roots))
        await db.execute(
            update(ListObservation)
            .where(
                ListObservation.subscription_id.in_(
                    select(ListSubscription.id).where(ListSubscription.list_id == list_id)
                ),
                ListObservation.work_id.in_(origins),
            )
            .values(excluded=True)
        )
        await db.execute(
            delete(ListEntry).where(ListEntry.list_id == list_id, ListEntry.work_id.in_(origins))
        )
        # Match the acquisition lock order used by ordinary list-reason withdrawal.
        for root in sorted(roots):
            await withdraw_list_reasons(db, user, list_id, root)
        changed = len(set(roots) & set(existing))
        changed_roots = set(roots) & set(existing)
    if not suppress_writeback:
        from app.domain.list_writeback import record_change

        for root in sorted(changed_roots):
            await record_change(db, user, list_id, root, body.action == "add")
    receipt = {"action": body.action, "selected": len(roots), "changed": changed}
    operation = Operation(
        owner_id=user.id,
        kind="lists.curate",
        idempotency_key=key,
        status="completed",
        message=f"{'Added' if body.action == 'add' else 'Removed'} {changed} list books",
        payload={"command": command, "receipt": receipt},
    )
    db.add(operation)
    db.add(AuditEvent(actor_id=user.id, action="lists.curated", entity_id=list_id, detail=receipt))
    await db.flush()
    return CurationReceipt(id=operation.id, **receipt)
