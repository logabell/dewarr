"""Read-only external-state observation; findings never authorize replay or resume."""

import asyncio
import hashlib
import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import delete, select, text

from app.db.models import (
    AcquisitionIntent,
    AcquisitionReason,
    AcquisitionReservation,
    AcquisitionSelection,
    AcquisitionTarget,
    AssetContains,
    AutomaticImport,
    AutomaticImportPolicy,
    BookList,
    CapacitySettings,
    CatalogAccount,
    DownloadAttempt,
    DownloadCapacity,
    DownloadHandoff,
    DownloadIdentityClaim,
    DownloadMembership,
    FrozenImportPlan,
    ImportCapacity,
    ImportDestination,
    ImportEntry,
    ImportRun,
    Integration,
    Library,
    LibraryAsset,
    LibraryGrant,
    ListAcquisitionBook,
    ListAcquisitionPolicy,
    ListCatalogBinding,
    ListEntry,
    ListObservation,
    ListSubscription,
    ListWritebackLease,
    ListWritebackPolicy,
    OidcIdentity,
    Operation,
    PlexIdentity,
    ProviderObject,
    RecoveryFinding,
    RecoveryScan,
    RestoreCheckpoint,
    SourceConnection,
    StorygraphAccount,
    User,
    Version,
    Work,
    WorkMetadataSource,
)
from app.db.session import session_factory
from app.domain.operations import transaction_lock
from app.importing.storage import storage_settings
from app.jobs.queue import enqueue
from app.jobs.retry import ShelfRetry
from app.security import decrypt_secrets

MAX_RECORDS = 10000
MAX_FINDINGS = 30000
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
logger = logging.getLogger(__name__)


class ScanHeld(RuntimeError):
    pass


def _digest_value(value):
    # Session rotation is not a configuration change. Username and shelf edits still are.
    accounts = value.get("storygraph_accounts") if isinstance(value, dict) else None
    if not isinstance(accounts, list):
        return value
    redacted = []
    for row in accounts:
        config = row.get("encrypted_config") if isinstance(row, dict) else None
        if not isinstance(config, str):
            redacted.append(row)
            continue
        try:
            saved = decrypt_secrets(config)
        except (InvalidToken, ValueError, TypeError):
            redacted.append(row)
            continue
        if not isinstance(saved, dict):
            redacted.append(row)
            continue
        stable = {key: item for key, item in saved.items() if key != "session_cookie"}
        redacted.append({**row, "encrypted_config": stable})
    return {**value, "storygraph_accounts": redacted}


def digest(value):
    payload = json.dumps(_digest_value(value), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


async def require_checkpoint(db, checkpoint_id, owner_id):
    checkpoint = await db.get(RestoreCheckpoint, checkpoint_id, populate_existing=True)
    actor = await db.get(User, owner_id, populate_existing=True)
    if (
        not checkpoint
        or not checkpoint.active
        or checkpoint.operator_id != owner_id
        or not actor
        or not actor.active
        or actor.role != "admin"
    ):
        raise ScanHeld("The active recovery operator or checkpoint changed")
    return checkpoint


async def context(db):
    result = {}
    for model in (
        User,
        Integration,
        SourceConnection,
        CatalogAccount,
        StorygraphAccount,
        Library,
        LibraryAsset,
        LibraryGrant,
        AssetContains,
        Work,
        Version,
        ProviderObject,
        AcquisitionSelection,
        DownloadAttempt,
        DownloadCapacity,
        DownloadIdentityClaim,
        DownloadHandoff,
        DownloadMembership,
        AutomaticImport,
        AutomaticImportPolicy,
        CapacitySettings,
        FrozenImportPlan,
        ImportCapacity,
        ImportDestination,
        ImportRun,
        ImportEntry,
        BookList,
        ListEntry,
        ListCatalogBinding,
        WorkMetadataSource,
        ListAcquisitionPolicy,
        ListAcquisitionBook,
        AcquisitionIntent,
        AcquisitionReason,
        AcquisitionReservation,
        AcquisitionTarget,
        ListSubscription,
        ListObservation,
        ListWritebackLease,
        ListWritebackPolicy,
    ):
        rows = (await db.scalars(select(model).limit(MAX_RECORDS + 1))).all()
        if len(rows) > MAX_RECORDS:
            raise ScanHeld("Recovery review exceeds the supported 10,000 records per entity type")
        result[model.__tablename__] = sorted(
            [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in rows
            ],
            key=lambda row: json.dumps(row, sort_keys=True, default=str),
        )
    identities = (await db.scalars(select(OidcIdentity).limit(MAX_RECORDS + 1))).all()
    if len(identities) > MAX_RECORDS:
        raise ScanHeld("Recovery review exceeds the supported 10,000 records per entity type")
    subjects = {row.user_id: row.subject for row in identities}
    plex_ids = {
        row.user_id: row.plex_user_id
        for row in (await db.scalars(select(PlexIdentity).limit(MAX_RECORDS + 1))).all()
    }
    if len(plex_ids) > MAX_RECORDS:
        raise ScanHeld("Recovery review exceeds the supported 10,000 records per entity type")
    for user in result["users"]:
        subject = subjects.get(user["id"])
        if subject:
            user["oidc_subject"] = subject
        plex_user_id = plex_ids.get(user["id"])
        if plex_user_id:
            user["plex_user_id"] = plex_user_id
    operations = (
        await db.scalars(
            select(Operation).where(Operation.kind == "lists.writeback").limit(MAX_RECORDS + 1)
        )
    ).all()
    if len(operations) > MAX_RECORDS:
        raise ScanHeld("Too much outbound history for this recovery review")
    result["outbound"] = [
        {"id": op.id, "owner_id": op.owner_id, "status": op.status, "payload": op.payload}
        for op in sorted(operations, key=lambda op: str(op.id))
    ]
    list_operations = list(
        await db.scalars(
            select(Operation)
            .where(
                Operation.kind.in_(
                    [
                        "lists.sync",
                        "lists.acquire",
                        "lists.requests",
                        "series.requests",
                        "series.acquire",
                        "lists.policy-preview",
                        "lists.writeback.compare",
                    ]
                )
            )
            .limit(MAX_RECORDS + 1)
        )
    )
    if len(list_operations) > MAX_RECORDS:
        raise ScanHeld("Too much list command history for this recovery review")
    result["list_operations"] = [
        {
            "id": op.id,
            "owner_id": op.owner_id,
            "kind": op.kind,
            "status": op.status,
            "payload": op.payload,
            "job_id": op.job_id,
        }
        for op in sorted(list_operations, key=lambda op: str(op.id))
    ]
    result["roots"] = (await storage_settings(db)).model_dump(
        mode="json",
        include={
            "import_sources",
            "import_destinations",
            "import_staging_root",
            "import_storage_routes",
            "import_journal_root",
            "hardcover_url",
            "openlibrary_url",
        },
    )
    return result


async def start(db, checkpoint, key):
    await transaction_lock(db, f"recovery:{checkpoint.id}")
    await require_checkpoint(db, checkpoint.id, checkpoint.operator_id)
    old = await db.scalar(
        select(Operation).where(
            Operation.owner_id == checkpoint.operator_id, Operation.idempotency_key == key
        )
    )
    if old:
        scan = await db.scalar(select(RecoveryScan).where(RecoveryScan.operation_id == old.id))
        if not scan or scan.checkpoint_id != checkpoint.id:
            raise HTTPException(409, "This command key belongs to another operation")
        return scan
    from app.domain.recovery_reconciliation import require_idle

    await require_idle(db, checkpoint.id)
    running = await db.scalar(
        select(RecoveryScan)
        .where(
            RecoveryScan.checkpoint_id == checkpoint.id,
            RecoveryScan.state.in_(["queued", "running"]),
        )
        .order_by(RecoveryScan.created_at.desc())
        .limit(1)
    )
    if running:
        operation = await db.get(Operation, running.operation_id)
        status = await db.scalar(
            text("SELECT status::text FROM book_queue.procrastinate_jobs WHERE id=:id"),
            {"id": operation.job_id},
        )
        if status in {"todo", "doing"}:
            raise HTTPException(409, "A recovery observation is already queued or running")
        running.state, operation.status = "held", "attention"
        operation.message = "The recovery worker stopped; a new observation is required"
    operation = Operation(
        owner_id=checkpoint.operator_id,
        kind="recovery.scan",
        idempotency_key=key,
        payload={"checkpoint_id": str(checkpoint.id)},
        message="Waiting for the read-only recovery worker",
    )
    db.add(operation)
    await db.flush()
    scan = RecoveryScan(checkpoint_id=checkpoint.id, operation_id=operation.id)
    db.add(scan)
    await db.flush()
    operation.job_id = await enqueue(db, "recovery.scan", scan_id=str(scan.id))
    return scan


class Writer:
    def __init__(self, scan_id, checkpoint_id, owner_id, token):
        self.scan_id, self.checkpoint_id, self.owner_id, self.token = (
            scan_id,
            checkpoint_id,
            owner_id,
            token,
        )
        self.count = 0

    async def pulse(self):
        async with session_factory()() as db, db.begin():
            await require_checkpoint(db, self.checkpoint_id, self.owner_id)
            scan = await db.get(RecoveryScan, self.scan_id, with_for_update=True)
            if scan.run_token != self.token or scan.state != "running":
                raise ScanHeld("The observation lease changed")
            scan.lease_until = datetime.now(UTC) + timedelta(minutes=2)

    async def add(self, domain, state, title, message, *, entity_id=None, evidence=None):
        if self.count >= MAX_FINDINGS:
            raise ScanHeld("The recovery report exceeds 30,000 findings")
        evidence = json.loads(json.dumps(evidence or {}, default=str))
        if len(json.dumps(evidence).encode()) > MAX_EVIDENCE_BYTES:
            raise ScanHeld("One recovery finding exceeds its evidence budget")
        await self.pulse()
        async with session_factory()() as db, db.begin():
            scan = await db.get(RecoveryScan, self.scan_id, with_for_update=True)
            await require_checkpoint(db, self.checkpoint_id, self.owner_id)
            if scan.run_token != self.token or scan.state != "running":
                raise ScanHeld("The observation lease changed")
            db.add(
                RecoveryFinding(
                    scan_id=scan.id,
                    position=self.count,
                    domain=domain,
                    state=state,
                    title=title[:600],
                    message=message[:600],
                    entity_id=entity_id,
                    evidence=evidence,
                )
            )
            self.count += 1


async def run(scan_id):
    from app.domain.recovery_observers import collect

    async with session_factory()() as db, db.begin():
        scan = await db.get(RecoveryScan, scan_id, with_for_update=True)
        if not scan or scan.state in {"completed", "held"}:
            return
        operation = await db.get(Operation, scan.operation_id)
        try:
            await require_checkpoint(db, scan.checkpoint_id, operation.owner_id)
            if scan.created_at < datetime.now(UTC) - timedelta(hours=1):
                raise ScanHeld("Recovery observation expired; start a fresh review")
            if scan.run_token and scan.lease_until and scan.lease_until > datetime.now(UTC):
                raise ShelfRetry(30)
            inputs = await context(db)
        except ScanHeld as error:
            scan.state, operation.status, operation.message = "held", "attention", str(error)
            scan.summary = {"message": str(error)}
            return
        token = uuid4()
        scan.context_digest = digest(inputs)
        scan.run_token, scan.state = token, "running"
        scan.lease_until = datetime.now(UTC) + timedelta(minutes=2)
        operation.status, operation.message = (
            "running",
            "Observing external state without changing downloads, files or lists",
        )
        await db.execute(delete(RecoveryFinding).where(RecoveryFinding.scan_id == scan.id))
        writer = Writer(scan.id, scan.checkpoint_id, operation.owner_id, token)
    held = None
    try:
        async with asyncio.timeout(900):
            await collect(inputs, writer)
    except (TimeoutError, ScanHeld) as error:
        held = (
            str(error)
            if isinstance(error, ScanHeld)
            else "Observation exceeded 15 minutes; no state has been reconciled"
        )
    except Exception as error:
        logger.error("Recovery observation %s failed (%s)", scan_id, type(error).__name__)
        held = "Observation could not finish; check recovery connections and filesystem access"
    async with session_factory()() as db, db.begin():
        scan = await db.get(RecoveryScan, scan_id, with_for_update=True)
        if not scan or scan.run_token != token:
            return
        operation = await db.get(Operation, scan.operation_id)
        try:
            await require_checkpoint(db, scan.checkpoint_id, operation.owner_id)
            if scan.context_digest != digest(await context(db)):
                raise ScanHeld(
                    "Restored configuration or workflow evidence changed during observation"
                )
        except ScanHeld as error:
            held = str(error)
        counts = Counter(
            await db.scalars(
                select(RecoveryFinding.state).where(RecoveryFinding.scan_id == scan.id)
            )
        )
        scan.summary = {
            "counts": dict(counts),
            "message": held or "Observation finished; findings do not authorize resume",
            "resume_available": False,
        }
        scan.state, operation.status = ("held", "attention") if held else ("completed", "completed")
        operation.message = scan.summary["message"]
        scan.run_token, scan.lease_until, scan.finished_at = None, None, datetime.now(UTC)
