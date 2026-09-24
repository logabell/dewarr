"""Reviewed source settings and durable verification of externally rotating sessions."""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select

from app.adapters.audiobookbay import endpoint as abb_endpoint
from app.adapters.contracts import AdapterError
from app.adapters.http import configured_url
from app.adapters.mam import cookie_value
from app.config import get_settings
from app.db.models import AuditEvent, Integration, Operation, RecoveryFinding, SourceConnection
from app.db.session import session_factory
from app.domain import recovery_reconciliation as reviews
from app.domain.audiobookbay_network import abb_call
from app.domain.operations import transaction_lock
from app.domain.prowlarr_network import prowlarr_call
from app.domain.recovery_scans import MAX_RECORDS, ScanHeld, context, digest
from app.domain.source_network import source_call
from app.jobs.queue import enqueue
from app.jobs.retry import ShelfRetry
from app.security import decrypt_secrets, encrypt_secrets

KIND = "recovery.sources"
TEST_KIND = "recovery.source-test"
NAMES = {"mam": "MyAnonamouse", "prowlarr": "Prowlarr", "audiobookbay": "AudiobookBay"}
SECRET_FIELDS = {"mam_id", "api_key", "proxy_username", "proxy_password"}


def credential_marker(value):
    return hmac.new(
        get_settings().encryption_key(),
        b"recovery.source-change\0" + json.dumps(value, sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()


class SourceChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: UUID
    source_key: Literal["mam", "prowlarr", "audiobookbay"]
    base_url: str = Field(max_length=2000)
    enabled: bool
    proxy_url: str | None = Field(default=None, max_length=2000)
    proxy_fallback_direct: bool = True
    mam_id: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    proxy_username: SecretStr | None = Field(default=None, min_length=1, max_length=300)
    proxy_password: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    clear_proxy_credentials: bool = False
    excluded_indexers: list[int] = Field(default_factory=list, max_length=1000)
    metadata_downloader_id: UUID | None = None

    @field_validator("base_url", "proxy_url")
    @classmethod
    def endpoint(cls, value):
        return configured_url(value) if value is not None else None

    @field_validator("mam_id")
    @classmethod
    def cookie(cls, value):
        return SecretStr(cookie_value(value.get_secret_value())) if value else None

    @field_validator("api_key")
    @classmethod
    def token(cls, value):
        if value and any(ord(c) < 33 or ord(c) > 126 for c in value.get_secret_value()):
            raise ValueError("Enter a valid API key without whitespace")
        return value

    @field_validator("excluded_indexers")
    @classmethod
    def indexers(cls, value):
        if any(i < 1 for i in value):
            raise ValueError("Indexer IDs must be positive")
        return sorted(set(value))

    @model_validator(mode="after")
    def correct_kind(self):
        if self.proxy_url and urlsplit(self.proxy_url).path:
            raise ValueError("Use a proxy origin without a path")
        if bool(self.proxy_username) != bool(self.proxy_password):
            raise ValueError("Enter both proxy username and password")
        if self.proxy_username and not self.proxy_url:
            raise ValueError("Configure a proxy before its credentials")
        if self.clear_proxy_credentials and self.proxy_username:
            raise ValueError("Choose replacement proxy credentials or clearing, not both")
        if self.source_key != "mam" and self.mam_id:
            raise ValueError("Only MAM accepts a mam_id")
        if self.source_key != "prowlarr" and (self.api_key or self.excluded_indexers):
            raise ValueError("Only Prowlarr accepts API keys and excluded indexers")
        if self.source_key == "prowlarr" and (self.proxy_url or self.clear_proxy_credentials):
            raise ValueError("Configure indexer proxy routing in Prowlarr")
        if self.source_key != "audiobookbay" and self.metadata_downloader_id:
            raise ValueError("Only AudiobookBay uses a metadata downloader")
        if self.source_key == "audiobookbay":
            self.base_url = abb_endpoint(self.base_url)
        return self

    def replacement(self):
        return {
            name: getattr(self, name).get_secret_value()
            for name in SECRET_FIELDS
            if getattr(self, name) is not None
        }

    def command(self):
        return {
            **self.model_dump(mode="json", exclude=SECRET_FIELDS),
            "credential_change": credential_marker(self.replacement()),
        }


def values(row):
    return {column.name: getattr(row, column.name) for column in SourceConnection.__table__.columns}


def signature(row, *, allow_rotation=False):
    secrets = decrypt_secrets(row["encrypted_secrets"])
    if allow_rotation and row["key"] == "mam":
        secrets.pop("mam_id", None)
    return digest(
        {
            **{
                k: row[k]
                for k in (
                    "key",
                    "base_url",
                    "proxy_url",
                    "proxy_fallback_direct",
                    "enabled",
                    "generation",
                )
            },
            "credentials": credential_marker(secrets),
        }
    )


def public_settings(row):
    secrets = decrypt_secrets(row["encrypted_secrets"])
    return {
        "source_key": row["key"],
        "base_url": row["base_url"],
        "enabled": row["enabled"],
        "proxy_url": row["proxy_url"],
        "proxy_fallback_direct": row["proxy_fallback_direct"],
        "has_credentials": bool(secrets.get("mam_id") or secrets.get("api_key")),
        "has_proxy_credentials": bool(secrets.get("proxy_password")),
        "excluded_indexers": secrets.get("excluded_indexers", [])
        if row["key"] == "prowlarr"
        else [],
        "metadata_downloader_id": secrets.get("metadata_downloader_id")
        if row["key"] == "audiobookbay"
        else None,
    }


async def confirmations(db, checkpoint_id):
    rows = list(
        await db.scalars(
            select(Operation)
            .where(
                Operation.kind == KIND,
                Operation.status == "completed",
                Operation.payload["checkpoint_id"].astext == str(checkpoint_id),
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(MAX_RECORDS + 1)
        )
    )
    if len(rows) > MAX_RECORDS:
        raise ScanHeld("Too much source-review history; operator review is required")
    found = {}
    for row in rows:
        for result in row.payload.get("results", []):
            found.setdefault(result["source_key"], result)
    return found


def current_session(row, confirmation):
    return bool(
        confirmation
        and confirmation["generation"] == row["generation"]
        and confirmation["current_session"]
        and not row["lease_token"]
    )


async def observe(inputs, writer):
    async with session_factory()() as db:
        history = await confirmations(db, writer.checkpoint_id)
        tests = {
            key: await db.get(Operation, UUID(item["test_id"]))
            for key, item in history.items()
            if item.get("test_id")
        }
    downloaders = [
        {"id": str(row["id"]), "name": row["name"], "enabled": row["enabled"]}
        for row in inputs["integrations"]
        if row["kind"] == "qbittorrent" and not row.get("deleted_at")
    ]
    for row in inputs["source_connections"]:
        if row.get("deleted_at"):
            continue
        if row["key"] not in NAMES:
            continue
        proof = tests.get(row["key"])
        verified = bool(
            proof
            and proof.status == "completed"
            and row["status"] == "connected"
            and not row["lease_token"]
            and proof.payload.get("verified_signature") == signature(row)
        )
        await writer.add(
            "review",
            "source-verified" if verified else "source-ready",
            "Source · " + NAMES[row["key"]],
            "Source connection verified; acquisition and automation remain paused"
            if verified
            else "Review source settings, then verify the connection while automation stays paused",
            evidence={
                "source_schema": 1,
                "before": public_settings(row),
                "requires_current_session": row["key"] == "mam"
                and not current_session(row, history.get("mam")),
                "downloaders": downloaders,
                "status": row["status"],
                "blocked_until": row["blocked_until"],
                "next_request_at": row["next_request_at"],
            },
        )


async def prepare(db, checkpoint, owner_id, scan_id, choice, key):
    old, scan, command = await reviews.review_inputs(
        db,
        checkpoint,
        owner_id,
        scan_id,
        [choice.finding_id],
        key,
        kind=KIND,
        extra_command={"change": choice.command()},
    )
    if old:
        return old
    finding = await db.get(RecoveryFinding, choice.finding_id)
    if (
        not finding
        or finding.scan_id != scan.id
        or finding.domain != "review"
        or finding.state not in {"source-ready", "source-verified"}
        or finding.evidence.get("source_schema") != 1
        or finding.evidence.get("before", {}).get("source_key") != choice.source_key
    ):
        raise HTTPException(409, "Choose a source from the current observation")
    row = await db.get(SourceConnection, choice.source_key)
    if not row or row.deleted_at:
        raise HTTPException(409, "Source settings changed; observe again")
    now = datetime.now(UTC)
    if row.lease_token and row.lease_until and row.lease_until > now:
        raise HTTPException(
            409, "Wait for the existing source request lease to expire before repair"
        )
    secrets = decrypt_secrets(row.encrypted_secrets)
    history = await confirmations(db, checkpoint.id)
    session_ok = current_session(values(row), history.get(row.key))
    proxy_auth_changed = (
        choice.clear_proxy_credentials
        and bool(secrets.get("proxy_password"))
        or choice.proxy_password is not None
        and any(
            choice.replacement().get(name) != secrets.get(name)
            for name in ("proxy_username", "proxy_password")
        )
    )
    route_changed = (
        row.base_url != choice.base_url
        or row.proxy_url != choice.proxy_url
        or row.proxy_fallback_direct != choice.proxy_fallback_direct
        or proxy_auth_changed
    )
    if (
        row.key == "mam"
        and not choice.mam_id
        and (
            row.base_url != choice.base_url
            or row.proxy_url != choice.proxy_url
            or (choice.enabled and (not session_ok or proxy_auth_changed))
        )
    ):
        raise HTTPException(422, "Enter a current mam_id for the restored or changed MAM route")
    if (
        row.key == "prowlarr"
        and not choice.api_key
        and (row.base_url != choice.base_url or (choice.enabled and not secrets.get("api_key")))
    ):
        raise HTTPException(422, "Enter a fresh API key for this Prowlarr endpoint")
    clear_proxy = choice.clear_proxy_credentials or row.proxy_url != choice.proxy_url
    if clear_proxy:
        secrets.pop("proxy_username", None)
        secrets.pop("proxy_password", None)
    secrets.update(choice.replacement())
    if row.key == "prowlarr":
        secrets["excluded_indexers"] = choice.excluded_indexers
    if row.key == "audiobookbay":
        if choice.metadata_downloader_id:
            downloader = await db.get(Integration, choice.metadata_downloader_id)
            if (
                not downloader
                or downloader.kind != "qbittorrent"
                or (choice.enabled and not downloader.enabled)
            ):
                raise HTTPException(422, "Choose an enabled qBittorrent metadata downloader")
        secrets["metadata_downloader_id"] = (
            str(choice.metadata_downloader_id) if choice.metadata_downloader_id else None
        )
    draft = {
        "key": row.key,
        "base_url": choice.base_url,
        "proxy_url": choice.proxy_url,
        "proxy_fallback_direct": choice.proxy_fallback_direct,
        "enabled": choice.enabled,
        "encrypted_secrets": encrypt_secrets(secrets),
    }
    item = {
        "finding_id": str(finding.id),
        "finding_digest": reviews.finding_signature(finding),
        "source_key": row.key,
        "source_signature": signature(values(row)),
        "before": public_settings(values(row)),
        "after": public_settings(draft),
        "replace_credentials": bool(choice.mam_id or choice.api_key),
        "proxy_credentials": "replace"
        if choice.proxy_password
        else "clear"
        if clear_proxy
        else "keep",
        "current_session": bool(choice.mam_id) or (session_ok and not route_changed),
        "clear_interrupted_lease": bool(choice.mam_id),
        "draft": draft,
    }
    return await reviews.save_review(
        db,
        checkpoint,
        owner_id,
        scan,
        command,
        [item],
        key,
        kind=KIND,
        message="Review source settings. Confirmation saves them, then verifies enabled "
        "connections; automation remains paused",
    )


async def read_current(identifier, token, payload):
    await reviews.pulse(identifier, token)
    return {item["finding_id"]: None for item in payload["items"]}


async def other_context(db, key):
    snapshot = await context(db)
    snapshot["source_connections"] = [
        row for row in snapshot["source_connections"] if row["key"] != key
    ]
    return digest(snapshot)


async def apply(db, review, item, observed):
    await transaction_lock(db, "source:" + item["source_key"])
    row = await db.get(
        SourceConnection, item["source_key"], populate_existing=True, with_for_update=True
    )
    if not row or row.deleted_at or signature(values(row)) != item["source_signature"]:
        raise HTTPException(409, "Source settings changed during review")
    if row.lease_token and row.lease_until and row.lease_until > datetime.now(UTC):
        raise HTTPException(409, "Another source request is still running")
    for name, value in item["draft"].items():
        setattr(row, name, value)
    row.generation += 1
    # A restored cookie can already have been consumed externally. Only an explicit
    # current cookie clears an interrupted session; source cooldowns survive repair.
    if item["clear_interrupted_lease"] or row.key != "mam":
        row.lease_token, row.lease_until = None, None
    row.status = "untested" if row.enabled else "disabled"
    row.last_error, row.last_success_at = None, None
    await db.flush()
    test = None
    if row.enabled:
        test = Operation(
            owner_id=review.owner_id,
            kind=TEST_KIND,
            idempotency_key=f"source-test:{review.id}",
            status="queued",
            message="Source settings saved; verification queued",
            payload={
                "checkpoint_id": review.payload["checkpoint_id"],
                "review_id": str(review.id),
                "source_key": row.key,
                "generation": row.generation,
                "source_signature": signature(values(row)),
                "rotation_binding": signature(values(row), allow_rotation=True),
                "context_digest": await other_context(db, row.key),
            },
        )
        db.add(test)
        await db.flush()
        test.job_id = await enqueue(db, TEST_KIND, operation_id=str(test.id))
    result = {
        "source_key": row.key,
        "generation": row.generation,
        "current_session": item["current_session"],
        "state": "saved" if row.enabled else "disabled",
        "test_id": str(test.id) if test else None,
    }
    db.add(
        AuditEvent(
            actor_id=review.owner_id,
            action="recovery.source.reviewed",
            entity_id=review.id,
            detail={
                "before": item["before"],
                "after": item["after"],
                "replace_credentials": item["replace_credentials"],
                "proxy_credentials": item["proxy_credentials"],
                **result,
            },
        )
    )
    return result


async def run(identifier):
    await reviews.run_review(
        identifier,
        kind=KIND,
        read=read_current,
        apply=apply,
        message="Source settings saved. Enabled connections have a separate verification result; "
        "automation remains paused.",
    )


async def test_guard(db, operation, *, token=None, after=False):
    payload = operation.payload
    await reviews.require_checkpoint(db, UUID(payload["checkpoint_id"]), operation.owner_id)
    parent = await db.get(Operation, UUID(payload["review_id"]), populate_existing=True)
    if (
        not parent
        or parent.kind != KIND
        or parent.status != "completed"
        or parent.owner_id != operation.owner_id
        or not any(r.get("test_id") == str(operation.id) for r in parent.payload.get("results", []))
    ):
        raise ScanHeld("The source settings review is no longer current")
    if token and (operation.status != "running" or payload.get("run_token") != str(token)):
        raise ScanHeld("Source verification lease changed")
    row = await db.get(SourceConnection, payload["source_key"], populate_existing=True)
    expected = payload["rotation_binding"] if after else payload["source_signature"]
    if (
        not row
        or row.deleted_at
        or not row.enabled
        or signature(values(row), allow_rotation=after) != expected
    ):
        raise ScanHeld("Source settings changed; observe and review again")
    if await other_context(db, row.key) != payload["context_digest"]:
        raise ScanHeld("Recovery context changed; observe and review again")
    return row


async def verify(identifier):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, identifier)
        if (
            not operation
            or operation.kind != TEST_KIND
            or operation.status not in {"queued", "running"}
        ):
            return
        await transaction_lock(db, "recovery:" + operation.payload["checkpoint_id"])
        await db.refresh(operation, with_for_update=True)
        if operation.status not in {"queued", "running"}:
            return
        if operation.status == "running":
            until = operation.payload.get("lease_until")
            if until and datetime.fromisoformat(until) > datetime.now(UTC):
                raise ShelfRetry(30)
            operation.status = "held"
            operation.message = (
                "Source verification was interrupted. Settings remain saved; "
                "observe and review the current connection before testing again."
            )
            operation.payload = {**operation.payload, "run_token": None, "lease_until": None}
            return
        try:
            await test_guard(db, operation)
        except (HTTPException, ScanHeld) as error:
            operation.status = "held"
            operation.message = (
                str(error.detail) if isinstance(error, HTTPException) else str(error)
            )
            return
        operation.status, operation.message = (
            "running",
            "Source settings saved; verifying the connection",
        )
        operation.payload = {
            **operation.payload,
            "run_token": str(token),
            "lease_until": (datetime.now(UTC) + timedelta(minutes=3)).isoformat(),
        }
        payload, owner_id = dict(operation.payload), operation.owner_id
    try:

        async def guard(db):
            current = await db.get(Operation, identifier, populate_existing=True)
            await test_guard(db, current, token=token)

        call = {"mam": source_call, "prowlarr": prowlarr_call, "audiobookbay": abb_call}[
            payload["source_key"]
        ]
        async with asyncio.timeout(120):
            await call(
                owner_id, "test", expected_generation=payload["generation"], recovery_guard=guard
            )
        async with session_factory()() as db, db.begin():
            await transaction_lock(db, "recovery:" + payload["checkpoint_id"])
            operation = await db.get(Operation, identifier, with_for_update=True)
            row = await test_guard(db, operation, token=token, after=True)
            if row.status != "connected" or row.lease_token:
                raise ScanHeld("The source did not complete verification")
            operation.status = "completed"
            operation.message = (
                "Source connection verified. Acquisition and automation remain paused."
            )
            operation.payload = {
                **operation.payload,
                "run_token": None,
                "lease_until": None,
                "verified_signature": signature(values(row)),
                "verified_at": datetime.now(UTC).isoformat(),
            }
            db.add(
                AuditEvent(
                    actor_id=owner_id,
                    action="recovery.source.verified",
                    entity_id=identifier,
                    detail={"source_key": row.key, "generation": row.generation},
                )
            )
    except Exception as error:
        if isinstance(error, (AdapterError, HTTPException, ScanHeld)):
            reason = str(error.detail) if isinstance(error, HTTPException) else str(error)
        elif isinstance(error, TimeoutError):
            reason = (
                "Connection verification timed out; "
                "an interrupted MAM session needs a current mam_id"
            )
        else:
            reason = "Connection verification failed; review the current source settings"
        async with session_factory()() as db, db.begin():
            operation = await db.get(Operation, identifier, with_for_update=True)
            if operation.payload.get("run_token") == str(token):
                operation.status, operation.message = "held", "Settings remain saved. " + reason
                operation.payload = {**operation.payload, "run_token": None, "lease_until": None}
