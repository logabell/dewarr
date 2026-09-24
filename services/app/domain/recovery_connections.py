"""Explicit repair of restored ABS/qBit settings; never activates saved work."""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select, update

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.grimmory import Grimmory
from app.adapters.http import configured_url
from app.adapters.qbittorrent import QbitClient, absolute_path
from app.config import get_settings
from app.db.models import AuditEvent, Integration, Library, Operation, RecoveryFinding
from app.db.session import session_factory
from app.domain import downloaders
from app.domain import recovery_reconciliation as reviews
from app.domain.operations import transaction_lock
from app.domain.recovery_scans import MAX_RECORDS, ScanHeld, digest
from app.importing.storage import import_sources
from app.security import decrypt_secrets, encrypt_secrets

KIND = "recovery.connections"
SUPPORTED = {"audiobookshelf", "grimmory", "qbittorrent"}
SECRET_FIELDS = {"token", "username", "password"}


class ConnectionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: UUID
    kind: Literal["audiobookshelf", "grimmory", "qbittorrent"]
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(max_length=2000)
    enabled: bool
    public_url: str | None = Field(default=None, max_length=2000)
    token: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    username: SecretStr | None = Field(default=None, min_length=1, max_length=300)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    save_path: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{0,100}$")
    mappings: list[downloaders.DownloadMapping] | None = Field(
        default=None, min_length=1, max_length=20
    )

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a connection name")
        return value.strip()

    @field_validator("base_url", "public_url")
    @classmethod
    def endpoint(cls, value):
        return configured_url(value) if value is not None else None

    @field_validator("save_path")
    @classmethod
    def path(cls, value):
        return absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def correct_kind(self):
        if self.kind == "audiobookshelf":
            if any(
                getattr(self, name) is not None
                for name in ("username", "password", "save_path", "category", "mappings")
            ):
                raise ValueError("Use Audiobookshelf settings for this connection")
        elif self.kind == "grimmory":
            if any(
                getattr(self, name) is not None
                for name in ("token", "save_path", "category", "mappings")
            ):
                raise ValueError("Use Grimmory settings for this connection")
            if bool(self.username) != bool(self.password):
                raise ValueError("Replace both the Grimmory username and password together")
        else:
            if self.token is not None or self.public_url is not None:
                raise ValueError("Use qBittorrent settings for this connection")
            if self.save_path is None or self.category is None or self.mappings is None:
                raise ValueError("Provide the downloader save path, category and path mappings")
            if bool(self.username) != bool(self.password):
                raise ValueError("Replace both the downloader username and password together")
        return self

    def replacement(self):
        if self.kind == "audiobookshelf":
            return {"token": self.token.get_secret_value()} if self.token else None
        if self.kind == "grimmory":
            return (
                {
                    "username": self.username.get_secret_value(),
                    "password": self.password.get_secret_value(),
                }
                if self.username and self.password
                else None
            )
        return (
            {
                "username": self.username.get_secret_value(),
                "password": self.password.get_secret_value(),
            }
            if self.username and self.password
            else None
        )

    def command(self):
        secret = self.replacement()
        # Stable replay comparison without storing plaintext or an unkeyed password digest.
        marker = (
            hmac.new(
                get_settings().encryption_key(),
                b"recovery.connection-change\0" + json.dumps(secret, sort_keys=True).encode(),
                hashlib.sha256,
            ).hexdigest()
            if secret
            else None
        )
        return {**self.model_dump(mode="json", exclude=SECRET_FIELDS), "credential_change": marker}


def values(row):
    return {column.name: getattr(row, column.name) for column in Integration.__table__.columns}


def signature(row):
    return digest(
        {
            key: row[key]
            for key in (
                "kind",
                "name",
                "base_url",
                "enabled",
                "config",
                "encrypted_secrets",
                "credential_generation",
            )
        }
    )


def public_settings(row):
    result = {key: row[key] for key in ("kind", "name", "base_url", "enabled")}
    result["has_credentials"] = bool(row["encrypted_secrets"])
    config = row["config"]
    if row["kind"] in {"audiobookshelf", "grimmory"}:
        result["public_url"] = config.get("public_url") or row["base_url"]
    else:
        result.update(
            save_path=config.get("save_path", ""),
            category=config.get("category", "book-search"),
            mappings=[
                {
                    "download_root": m["download_root"],
                    "source_key": m["source_key"],
                    "worker_path": m["source_path"],
                }
                for m in config.get("mappings", [])
            ],
        )
    return result


async def observe(inputs, writer):
    async with session_factory()() as db:
        history = list(
            await db.scalars(
                select(Operation)
                .where(
                    Operation.kind == KIND,
                    Operation.status == "completed",
                    Operation.payload["checkpoint_id"].astext == str(writer.checkpoint_id),
                )
                .order_by(Operation.created_at.desc(), Operation.id.desc())
                .limit(MAX_RECORDS + 1)
            )
        )
        roots = [
            {"key": key, "path": str(path)}
            for key, path in sorted((await import_sources(db)).items())
        ]
    if len(history) > MAX_RECORDS:
        raise ScanHeld("Too much connection-review history; operator review is required")
    confirmations = {}
    for operation in history:
        for result in operation.payload.get("results", []):
            confirmations.setdefault(result["integration_id"], result["connection_digest"])
    for row in inputs["integrations"]:
        if row.get("deleted_at") or row["kind"] not in SUPPORTED or row["owner_id"] is not None:
            continue
        confirmed = confirmations.get(str(row["id"])) == signature(row)
        await writer.add(
            "review",
            "connection-reviewed" if confirmed else "connection-ready",
            "Connection · " + row["name"],
            "Connection settings reviewed; inventory, file routes and activation remain separate"
            if confirmed
            else "Review the current endpoint, credentials and routing settings",
            entity_id=row["id"],
            evidence={
                "connection_schema": 1,
                "before": public_settings(row),
                "source_roots": roots,
            },
        )


async def unique_endpoint(db, identifier, kind, endpoint):
    if kind == "qbittorrent" and await db.scalar(
        select(Integration.id).where(
            Integration.kind == kind,
            Integration.deleted_at.is_(None),
            Integration.base_url == endpoint,
            Integration.id != identifier,
        )
    ):
        raise HTTPException(409, "This downloader endpoint already has a connection")


def draft_settings(row, choice, sources):
    replacement = choice.replacement()
    if row.base_url != choice.base_url and replacement is None:
        raise HTTPException(422, "Enter fresh credentials before changing a connection endpoint")
    if choice.enabled and not row.encrypted_secrets and replacement is None:
        raise HTTPException(422, "Enter credentials before enabling this connection")
    config = dict(row.config)
    if choice.kind in {"audiobookshelf", "grimmory"}:
        public_url = choice.public_url or choice.base_url
        if public_url != (config.get("public_url") or row.base_url):
            config["public_url"] = public_url
    else:
        proposed = [
            {"download_root": mapping.download_root, "source_key": mapping.source_key}
            for mapping in choice.mappings
        ]
        previous = [
            {key: m[key] for key in ("download_root", "source_key")}
            for m in config.get("mappings", [])
        ]
        # An unreachable/missing old mount must not prevent deliberately disabling it.
        mappings = (
            config.get("mappings", [])
            if not choice.enabled
            and proposed == previous
            and choice.save_path == config.get("save_path")
            else downloaders.bind_mappings(choice.mappings, choice.save_path, sources)[0]
        )
        config = {
            **config,
            "save_path": choice.save_path,
            "category": choice.category,
            "mappings": mappings,
        }
    return {
        "kind": choice.kind,
        "name": choice.name,
        "base_url": choice.base_url,
        "enabled": choice.enabled,
        "config": config,
        "encrypted_secrets": encrypt_secrets(replacement)
        if replacement is not None
        else row.encrypted_secrets,
    }


async def prepare(db, checkpoint, owner_id, scan_id, choices, key):
    if not 1 <= len(choices) <= 10:
        raise HTTPException(422, "Review one to ten connections together")
    changes = sorted(choices, key=lambda c: str(c.finding_id))
    old, scan, command = await reviews.review_inputs(
        db,
        checkpoint,
        owner_id,
        scan_id,
        [c.finding_id for c in changes],
        key,
        kind=KIND,
        extra_command={"changes": [c.command() for c in changes]},
    )
    if old:
        return old
    items, seen, endpoints = [], set(), set()
    for choice in changes:
        finding = await db.get(RecoveryFinding, choice.finding_id)
        if (
            not finding
            or finding.scan_id != scan.id
            or finding.domain != "review"
            or finding.state not in {"connection-ready", "connection-reviewed"}
            or finding.evidence.get("connection_schema") != 1
            or not finding.entity_id
            or finding.entity_id in seen
        ):
            raise HTTPException(409, "Choose each connection from the current observation once")
        row = await db.get(Integration, finding.entity_id)
        if not row or row.deleted_at or row.kind != choice.kind or row.owner_id is not None:
            raise HTTPException(409, "The selected connection changed; observe again")
        await unique_endpoint(db, row.id, row.kind, choice.base_url)
        if row.kind == "qbittorrent" and choice.base_url in endpoints:
            raise HTTPException(409, "Choose a distinct endpoint for each downloader")
        if row.kind == "qbittorrent":
            endpoints.add(choice.base_url)
        draft = draft_settings(row, choice, await import_sources(db))
        seen.add(row.id)
        items.append(
            {
                "finding_id": str(finding.id),
                "finding_digest": reviews.finding_signature(finding),
                "integration_id": str(row.id),
                "connection_signature": signature(values(row)),
                "before": public_settings(values(row)),
                "after": public_settings(draft),
                "replace_credentials": choice.replacement() is not None,
                "draft": draft,
            }
        )
    return await reviews.save_review(
        db,
        checkpoint,
        owner_id,
        scan,
        command,
        items,
        key,
        kind=KIND,
        message="Review these settings. Enabled connections are tested before saving; "
        "automation remains paused",
    )


async def read_current(identifier, token, payload):
    observed = {}
    for item in payload["items"]:
        await reviews.pulse(identifier, token)
        draft = item["draft"]
        capabilities = {}
        if draft["enabled"]:
            credentials = decrypt_secrets(draft["encrypted_secrets"])
            async with asyncio.timeout(60):
                if draft["kind"] == "audiobookshelf":
                    async with Audiobookshelf(draft["base_url"], credentials["token"]) as client:
                        result, _ = await client.authorize()
                        await client.libraries()
                elif draft["kind"] == "grimmory":
                    async with Grimmory(
                        draft["base_url"],
                        {
                            "username": credentials["username"],
                            "password": credentials["password"],
                        },
                    ) as client:
                        result, _ = await client.authorize()
                        await client.libraries()
                else:
                    async with QbitClient(
                        draft["base_url"], credentials["username"], credentials["password"]
                    ) as client:
                        result = await client.capabilities()
            capabilities = result.model_dump(mode="json")
        observed[item["finding_id"]] = capabilities
    return observed


async def apply(db, review, item, capabilities):
    await transaction_lock(db, downloaders.SETTINGS_LOCK)
    row = await db.get(
        Integration, UUID(item["integration_id"]), populate_existing=True, with_for_update=True
    )
    if not row or row.deleted_at or signature(values(row)) != item["connection_signature"]:
        raise HTTPException(409, "Connection settings changed while the review was running")
    draft = item["draft"]
    await unique_endpoint(db, row.id, row.kind, draft["base_url"])
    changed = any(getattr(row, key) != value for key, value in draft.items())
    if changed:
        for key, value in draft.items():
            setattr(row, key, value)
        row.credential_generation += 1
        row.lease_token, row.lease_until, row.next_sync_at = None, None, None
        if row.kind in {"audiobookshelf", "grimmory"}:
            await db.execute(
                update(Library).where(Library.integration_id == row.id).values(accessible=False)
            )
    row.status = "connected" if row.enabled else "disabled"
    row.last_error = None
    row.last_success_at = datetime.now(UTC) if row.enabled else None
    row.capabilities = capabilities
    await db.flush()
    result = {
        "integration_id": str(row.id),
        "changed": changed,
        "state": "tested" if row.enabled else "disabled",
        "connection_digest": signature(values(row)),
    }
    db.add(
        AuditEvent(
            actor_id=review.owner_id,
            action="recovery.connection.reviewed",
            entity_id=row.id,
            detail={
                "review_id": str(review.id),
                "before": item["before"],
                "after": item["after"],
                "replace_credentials": item["replace_credentials"],
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
        message="Connection settings reviewed. Reconcile current inventory and verify file routes "
        "before activation; automation remains paused.",
    )
