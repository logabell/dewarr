"""Durable, explicitly authorized Hardcover membership writes.

Local-list locks protect commands; short-lived remote-list leases fence network
attempts across different local bindings. Unknown external effects are observed,
never blindly replayed. No playback, reading-status or file mutation lives here.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import Integer, Text, cast, select, text

from app.adapters.catalog_providers import Hardcover
from app.adapters.contracts import AdapterError, FailureKind, MutationError
from app.adapters.hardcover_writeback import (
    Membership,
    Observation,
    decide,
    mutate,
    observe,
    owned_list,
)
from app.config import get_settings
from app.db.models import (
    AuditEvent,
    BookList,
    CatalogAccount,
    ListCatalogBinding,
    ListEntry,
    ListObservation,
    ListSubscription,
    ListWritebackLease,
    ListWritebackPolicy,
    Operation,
    User,
    Work,
    WorkMetadataSource,
)
from app.db.session import session_factory
from app.domain.catalog_network import CatalogGateway
from app.domain.operations import transaction_lock
from app.domain.visibility import visible_origin_work, visible_work
from app.domain.work_graph import canonical_work, family_ids, graph_lock
from app.jobs.queue import enqueue
from app.jobs.retry import ShelfRetry
from app.security import decrypt_secrets

KIND = "lists.writeback"
TERMINAL = {"completed", "attention", "paused", "superseded"}


def records(list_id):
    return (Operation.kind == KIND, Operation.payload["list_id"].astext == str(list_id))


async def require_reconciled_before_detach(db, list_id):
    """Caller holds the list lock; preserve access to uncertain remote history."""
    pending = await db.scalar(
        select(Operation.id)
        .where(
            *records(list_id),
            Operation.payload["pending_attempt"].astext.is_not(None),
        )
        .limit(1)
    )
    if pending:
        raise HTTPException(
            409,
            "A Hardcover change is still unconfirmed. Check its remote state before "
            "deleting this list or detaching its subscription.",
        )


async def repair_job(db, operation):
    if operation.status not in {"queued", "running"}:
        return
    status = (
        await db.scalar(
            text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
            {"id": operation.job_id},
        )
        if operation.job_id
        else None
    )
    if status not in {"todo", "doing"}:
        operation.status = "attention"
        operation.message = "Write-back worker ended; check remote state before reviewing a retry"


async def context(db, owner_id, list_id):
    item = await db.scalar(select(BookList).where(BookList.id == list_id).with_for_update())
    # Match account-save lock order before the actor's FK/key-share lock.
    await transaction_lock(db, f"catalog-account:{owner_id}")
    owner = await db.scalar(
        select(User)
        .where(User.id == owner_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if (
        not item
        or not owner
        or item.owner_id != owner_id
        or not owner.active
        or owner.role == "viewer"
    ):
        raise HTTPException(404, "Editable list not found")
    account = await db.get(CatalogAccount, owner_id, populate_existing=True)
    subscription = await db.scalar(
        select(ListSubscription).where(ListSubscription.list_id == list_id)
    )
    policy = await db.get(ListWritebackPolicy, list_id, populate_existing=True)
    return item, owner, account, subscription, policy


def binding(account, subscription):
    if not account or not account.enabled:
        raise HTTPException(409, "Connect your Hardcover account before configuring write-back")
    if not subscription or subscription.provider != "hardcover":
        raise HTTPException(409, "Follow a Hardcover list before configuring write-back")
    config = decrypt_secrets(subscription.encrypted_config)
    if config.get("source_kind"):
        raise HTTPException(422, "Author and series catalogs are read-only sources")
    if not config.get("complete") or not subscription.baseline_at:
        raise HTTPException(
            409, "Complete the first Hardcover list observation before enabling write-back"
        )
    return int(config["external_id"])


async def fetch_owner(owner_id, generation, token, external_list_id):
    async with asyncio.timeout(45):
        async with CatalogGateway(
            "hardcover", f"{owner_id}:{generation}", token, cache=False
        ) as gateway:
            return await owned_list(Hardcover(gateway.request).query, external_list_id)


async def fetch_membership(owner_id, generation, token, external_list_id, book_id):
    async with asyncio.timeout(45):
        async with CatalogGateway(
            "hardcover", f"{owner_id}:{generation}", token, cache=False
        ) as gateway:
            return await observe(Hardcover(gateway.request).query, external_list_id, book_id)


async def send_membership(owner_id, generation, token, observation, decision):
    async with asyncio.timeout(45):
        async with CatalogGateway(
            "hardcover", f"{owner_id}:{generation}", token, cache=False
        ) as gateway:
            return await mutate(gateway.mutate, observation, decision)


async def external_identity(db, owner, work_id, subscription):
    """Only existing accepted identities; titles never authorize an external write."""
    origins = family_ids(work_id)
    observed = list(
        await db.scalars(
            select(ListObservation).where(
                ListObservation.subscription_id == subscription.id,
                ListObservation.work_id.in_(origins),
            )
        )
    )
    keys = {o.external_id for o in observed if not o.snapshot.get("identity_changed")}
    sources = await db.scalars(
        select(WorkMetadataSource.external_id)
        .join(Work)
        .where(
            WorkMetadataSource.work_id.in_(origins),
            WorkMetadataSource.provider == "hardcover",
            WorkMetadataSource.accepted.is_(True),
            visible_origin_work(owner),
        )
    )
    keys.update(sources)
    bindings = await db.scalars(
        select(ListCatalogBinding.identity_key).where(
            ListCatalogBinding.owner_id == owner.id,
            ListCatalogBinding.work_id.in_(origins),
            ListCatalogBinding.identity_key.like("hardcover:%"),
        )
    )
    keys.update(value.removeprefix("hardcover:") for value in bindings)
    if len(keys) != 1:
        return None, None
    value = next(iter(keys))
    if not value.isdecimal() or not 1 <= int(value) <= 2147483647:
        return None, None
    matching = next((o for o in observed if o.external_id == value), None)
    if matching and matching.snapshot.get("identity_changed"):
        return None, None
    return int(value), matching


async def episode(db, list_id, work_id):
    ids = await db.scalars(
        select(ListEntry.id)
        .where(
            ListEntry.list_id == list_id,
            ListEntry.work_id.in_(family_ids(work_id)),
        )
        .order_by(ListEntry.id)
    )
    return [str(value) for value in ids]


async def record_change(db, owner, list_id, work_id, desired, *, base=None):
    """Caller holds the list/actor/graph locks; enqueue with the local edit."""
    policy = await db.get(ListWritebackPolicy, list_id)
    if not policy or not policy.enabled:
        return None
    subscription = (
        await db.get(ListSubscription, policy.subscription_id) if policy.subscription_id else None
    )
    if not subscription or subscription.provider != "hardcover":
        return None
    await db.flush()
    work = await canonical_work(db, work_id)
    book_id, observed = await external_identity(db, owner, work.id, subscription)
    if book_id and base is None:
        previous = await db.scalar(
            select(Operation)
            .where(
                *records(list_id),
                Operation.owner_id == owner.id,
                Operation.payload["book_id"].as_integer() == book_id,
                Operation.payload["account_generation"].as_integer() == policy.account_generation,
                Operation.payload["external_list_id"].as_integer() == policy.external_list_id,
                Operation.payload["confirmed_at"].astext.is_not(None),
            )
            .order_by(cast(Operation.payload["sequence"].astext, Integer).desc())
            .limit(1)
        )
        if previous and (
            not observed
            or datetime.fromisoformat(previous.payload["confirmed_at"]) > observed.last_seen_at
        ):
            base = Observation.model_validate_json(json.dumps(previous.payload["last_observation"]))
    members = []
    if observed and observed.present:
        members = [
            Membership(
                id=entry["entry_id"],
                list_id=policy.external_list_id,
                book_id=book_id,
                edition_id=int(entry["edition_id"]) if entry.get("edition_id") else None,
            )
            for entry in observed.snapshot.get("memberships", [])
        ]
    if book_id and base is None:
        base = Observation(
            list_id=policy.external_list_id,
            owner_id=policy.remote_owner_id,
            book_id=book_id,
            memberships=tuple(sorted(members, key=lambda e: e.id)),
        )
    if base and (base.list_id, base.owner_id, base.book_id) != (
        policy.external_list_id,
        policy.remote_owner_id,
        book_id,
    ):
        raise HTTPException(409, "Hardcover identity changed; review the current membership")
    policy.sequence += 1
    payload = {
        "list_id": str(list_id),
        "subscription_id": str(subscription.id),
        "policy_generation": policy.generation,
        "account_generation": policy.account_generation,
        "remote_owner_id": policy.remote_owner_id,
        "external_list_id": policy.external_list_id,
        "work_id": str(work.id),
        "book_id": book_id,
        "desired": desired,
        "sequence": policy.sequence,
        "episode": await episode(db, list_id, work.id),
        "base": base.model_dump(mode="json") if base else None,
        "pending_attempt": None,
        "attempts": 0,
        "observations": 0,
    }
    operation = Operation(
        owner_id=owner.id,
        kind=KIND,
        idempotency_key=f"writeback:{uuid4()}",
        payload=payload,
        status="queued" if book_id else "attention",
        message="Waiting to synchronize this Hardcover membership"
        if book_id
        else "Match this book to one Hardcover identity before sending its list change",
    )
    db.add(operation)
    await db.flush()
    if book_id:
        operation.job_id = await enqueue(db, KIND, operation_id=str(operation.id))
    return operation


def target(payload):
    return f"hardcover:{payload['remote_owner_id']}:{payload['external_list_id']}"


async def acquire_lease(db, operation):
    key = target(operation.payload)
    await transaction_lock(db, f"list-writeback:{key}")
    lease = await db.get(ListWritebackLease, key, populate_existing=True)
    now = datetime.now(UTC)
    if lease and lease.lease_until > now:
        raise ShelfRetry((lease.lease_until - now).total_seconds() + 1)
    token = uuid4()
    if not lease:
        lease = ListWritebackLease(target=key)
        db.add(lease)
    lease.operation_id, lease.token, lease.lease_until = (
        operation.id,
        token,
        now + timedelta(minutes=2),
    )
    return token


async def release_lease(db, operation, token):
    lease = await db.get(ListWritebackLease, target(operation.payload))
    if lease and lease.operation_id == operation.id and lease.token == token:
        lease.lease_until = datetime.now(UTC)


async def load_operation(db, operation_id):
    operation = await db.get(Operation, operation_id)
    if not operation or operation.kind != KIND or operation.status in TERMINAL:
        return None
    try:
        ctx = await context(db, operation.owner_id, UUID(operation.payload["list_id"]))
    except HTTPException:
        await db.refresh(operation)
        operation.status, operation.message = (
            "attention",
            "List access changed; any sent write needs reconciliation",
        )
        return None
    await db.refresh(operation)
    return operation, ctx


async def authority(db, operation, ctx):
    _, owner, account, subscription, policy = ctx
    p = operation.payload
    if (
        not account
        or not account.enabled
        or account.generation != p["account_generation"]
        or not policy
        or not policy.enabled
        or policy.generation != p["policy_generation"]
        or not subscription
        or str(subscription.id) != p["subscription_id"]
        or policy.subscription_id != subscription.id
    ):
        return "Write-back settings or account changed; review this pending change"
    await graph_lock(db)
    work = await canonical_work(db, UUID(p["work_id"]))
    if not await db.scalar(select(Work.id).where(Work.id == work.id, visible_work(owner))):
        return "Book access changed; review this pending change"
    if await episode(db, policy.list_id, work.id) != p["episode"]:
        return "Local membership changed; review the newer list state"
    newer = await db.scalar(
        select(Operation.id)
        .where(
            *records(policy.list_id),
            Operation.payload["work_id"].astext.in_(
                select(cast(Work.id, Text)).where(Work.id.in_(family_ids(work.id)))
            ),
            cast(Operation.payload["sequence"].astext, Integer) > p["sequence"],
        )
        .limit(1)
    )
    if newer:
        return "A newer local membership change supersedes this command"
    identity, _ = await external_identity(db, owner, work.id, subscription)
    if identity != p["book_id"]:
        return "Hardcover book identity changed; review before writing"
    return None


async def run(operation_id):
    if get_settings().recovery_mode:
        raise ShelfRetry(60)
    async with session_factory()() as db, db.begin():
        loaded = await load_operation(db, operation_id)
        if not loaded:
            return
        operation, ctx = loaded
        if operation.status in TERMINAL:
            return
        error = await authority(db, operation, ctx)
        can_observe_sent = bool(
            (operation.payload["pending_attempt"] or operation.payload.get("reconcile_only"))
            and ctx[2]
            and ctx[2].enabled
            and (
                ctx[2].generation == operation.payload["account_generation"]
                or operation.payload.get("reconcile_only")
            )
        )
        if error and not can_observe_sent:
            operation.status, operation.message = "attention", error
            return
        if operation.created_at < datetime.now(UTC) - timedelta(
            days=7
        ) and not operation.payload.get("reconcile_only"):
            operation.status, operation.message = (
                "attention",
                "Pending Hardcover change expired; review current membership",
            )
            return
        token = await acquire_lease(db, operation)
        p = dict(operation.payload)
        account = ctx[2]
        secret = decrypt_secrets(account.encrypted_token)["token"]
        owner_id, generation = operation.owner_id, account.generation
        operation.status, operation.message = "running", "Checking current Hardcover membership"
    try:
        current = await fetch_membership(
            owner_id, generation, secret, p["external_list_id"], p["book_id"]
        )
    except (AdapterError, TimeoutError) as error:
        async with session_factory()() as db, db.begin():
            loaded = await load_operation(db, operation_id)
            if not loaded:
                return
            operation, _ = loaded
            lease = await db.get(ListWritebackLease, target(p), populate_existing=True)
            if not lease or lease.token != token or lease.operation_id != operation.id:
                return
            await release_lease(db, operation, token)
            operation.payload = {
                **operation.payload,
                "observations": operation.payload["observations"] + 1,
            }
            operation.message = (
                str(error)
                if isinstance(error, AdapterError)
                else "Hardcover membership check timed out"
            )
            operation.status = "attention" if operation.payload["observations"] >= 5 else "queued"
            retry = operation.status == "queued"
        if retry:
            raise ShelfRetry(getattr(error, "retry_after", None) or 60) from None
        return
    async with session_factory()() as db, db.begin():
        loaded = await load_operation(db, operation_id)
        if not loaded:
            return
        operation, ctx = loaded
        lease = await db.get(ListWritebackLease, target(p), populate_existing=True)
        if not lease or lease.token != token or lease.operation_id != operation.id:
            return
        error = await authority(db, operation, ctx)
        if error:
            attempt = operation.payload["pending_attempt"]
            same_target = (current.owner_id, current.list_id, current.book_id) == (
                p["remote_owner_id"],
                p["external_list_id"],
                p["book_id"],
            )
            applied = (
                attempt
                and same_target
                and (
                    (attempt["action"] == "add" and bool(current.memberships))
                    or (
                        attempt["action"] == "remove"
                        and attempt["entry_id"] not in {e.id for e in current.memberships}
                    )
                )
            )
            operation.payload = {
                **operation.payload,
                "last_observation": current.model_dump(mode="json"),
                "pending_attempt": None if applied else attempt,
            }
            operation.status, operation.message = "attention", error
            await release_lease(db, operation, token)
            return
        p = dict(operation.payload)
        base = Observation.model_validate_json(json.dumps(p["base"]))
        attempt = p["pending_attempt"]
        # An observed deletion of the exact attempted row can release the next
        # independently identified edition membership in this same command.
        if (
            attempt
            and attempt["action"] == "remove"
            and attempt["entry_id"] not in {e.id for e in current.memberships}
        ):
            attempt = None
            p["pending_attempt"] = None
        decision = decide(base, current, p["desired"], uncertain=bool(attempt))
        p["last_observation"] = current.model_dump(mode="json")
        p["observations"] += 1
        if decision.action == "confirmed":
            operation.status, operation.message = "completed", decision.message
            p["pending_attempt"] = None
            p["confirmed_at"] = datetime.now(UTC).isoformat()
            ctx[4].confirmed_at = datetime.now(UTC)
            db.add(
                AuditEvent(
                    actor_id=owner_id, action="list.writeback.confirmed", entity_id=operation.id
                )
            )
        elif decision.action in {"conflict", "reconcile"}:
            operation.status = (
                "queued"
                if decision.action == "reconcile" and p["observations"] < 5
                else "attention"
            )
            operation.message = decision.message
        elif p.get("reconcile_only"):
            operation.status, operation.message = (
                "attention",
                "Remote membership differs; review before applying local state",
            )
        else:
            # Do not race a still-unknown command from another local binding.
            unresolved = await db.scalar(
                select(Operation.id)
                .where(
                    Operation.kind == KIND,
                    Operation.id != operation.id,
                    Operation.payload["remote_owner_id"].as_integer() == p["remote_owner_id"],
                    Operation.payload["external_list_id"].as_integer() == p["external_list_id"],
                    Operation.payload["book_id"].as_integer() == p["book_id"],
                    Operation.payload["pending_attempt"].astext.is_not(None),
                )
                .limit(1)
            )
            if unresolved:
                operation.status, operation.message = (
                    "attention",
                    "An earlier write for this book needs reconciliation",
                )
            else:
                p["pending_attempt"] = {
                    "action": decision.action,
                    "entry_id": decision.entry_id,
                    "sent_at": datetime.now(UTC).isoformat(),
                }
                p["attempts"] += 1
                operation.message = "Sending the reviewed Hardcover membership change"
        operation.payload = p
        send = operation.status == "running" and decision.action in {"add", "remove"}
        if not send:
            await release_lease(db, operation, token)
            retry = operation.status == "queued"
    if not send:
        if retry:
            raise ShelfRetry(60)
        return
    error = None
    try:
        await send_membership(owner_id, generation, secret, current, decision)
    except (AdapterError, TimeoutError) as caught:
        error = caught
    async with session_factory()() as db, db.begin():
        loaded = await load_operation(db, operation_id)
        if not loaded:
            return
        operation, _ = loaded
        lease = await db.get(ListWritebackLease, target(p), populate_existing=True)
        if not lease or lease.token != token or lease.operation_id != operation.id:
            return
        p = dict(operation.payload)
        if isinstance(error, MutationError) and not error.may_have_applied:
            p["pending_attempt"] = None
            operation.status = (
                "queued"
                if error.kind == FailureKind.RATE_LIMIT and p["attempts"] < 5
                else "attention"
            )
            operation.message = str(error)
        else:
            operation.status = "queued"
            operation.message = "Checking whether Hardcover applied this membership change"
        operation.payload = p
        await release_lease(db, operation, token)
        retry = operation.status == "queued"
    if retry:
        raise ShelfRetry(getattr(error, "retry_after", None) or 5)
