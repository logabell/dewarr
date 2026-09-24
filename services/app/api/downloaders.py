from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.adapters.contracts import AdapterError
from app.adapters.http import configured_url
from app.adapters.qbittorrent import absolute_path
from app.api.dependencies import Admin, Database
from app.api.metadata import adapter_http_error
from app.config import get_settings
from app.db.models import AuditEvent, ImportStorageSettings, Integration
from app.domain import downloaders
from app.domain.download_folders import browse_folders
from app.domain.downloaders import DownloadMapping
from app.domain.operations import transaction_lock
from app.domain.source_network import check_actor
from app.importing.storage import import_sources, storage_settings
from app.security import decrypt_secrets, encrypt_secrets

router = APIRouter(prefix="/downloaders", tags=["downloaders"])


class DownloaderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["qbittorrent", "transmission", "deluge", "sabnzbd", "nzbget"] = "qbittorrent"
    name: str = Field(default="qBittorrent", min_length=1, max_length=120)
    base_url: str = Field(max_length=2000)
    username: SecretStr | None = Field(default=None, min_length=1, max_length=300)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    save_path: str | None = Field(default=None, max_length=2000)
    category: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,100}$")
    mappings: list[DownloadMapping] | None = Field(default=None, max_length=20)
    enabled: bool = True
    expected_generation: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def name_value(cls, value):
        if not value.strip():
            raise ValueError("Enter a connection name")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        value = value.strip()
        return configured_url(value if "://" in value else "http://" + value)

    @field_validator("api_key")
    @classmethod
    def token(cls, value):
        secret = value.get_secret_value() if value else ""
        if secret and any(ord(character) < 33 or ord(character) > 126 for character in secret):
            raise ValueError("Enter a valid API key without whitespace")
        return value

    @field_validator("save_path")
    @classmethod
    def path(cls, value):
        return absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def legacy_storage(self):
        if self.save_path is not None and not self.mappings:
            raise ValueError("Legacy storage settings must be supplied together")
        return self


class DownloaderMappingView(DownloadMapping):
    worker_path: str


class DownloaderView(BaseModel):
    id: UUID
    kind: Literal["qbittorrent", "transmission", "deluge", "sabnzbd", "nzbget", "slskd"]
    name: str
    base_url: str
    enabled: bool
    has_credentials: bool
    generation: int
    status: str
    last_error: str | None
    last_success_at: datetime | None
    version: str | None
    save_path: str
    category: str
    mappings: list[DownloaderMappingView]
    mappings_current: bool
    dispatch_available: bool = False
    capabilities: dict[str, bool] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class PathPreviewInput(BaseModel):
    path: str = Field(max_length=2000)
    expected_generation: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        return absolute_path(value)


class PathPreviewView(BaseModel):
    download_path: str
    source_key: str
    relative_path: str
    worker_path: str
    filesystem_verified: bool = False


def view(row, sources):
    return DownloaderView(
        id=row.id,
        kind=row.kind,
        name=row.name,
        base_url=row.base_url,
        enabled=row.enabled,
        has_credentials=any(decrypt_secrets(row.encrypted_secrets).values()),
        generation=row.credential_generation,
        status=row.status,
        last_error=row.last_error,
        last_success_at=row.last_success_at,
        version=row.capabilities.get("version"),
        save_path=row.config["save_path"],
        category=row.config["category"],
        mappings=[
            DownloaderMappingView(
                download_root=mapping["download_root"],
                source_key=mapping["source_key"],
                worker_path=mapping["source_path"],
            )
            for mapping in row.config["mappings"]
        ],
        mappings_current=downloaders.mappings_current(row, sources),
        capabilities=downloaders.client_features(row),
        limitations=row.capabilities.get("limitations", []),
    )


@router.get("", response_model=list[DownloaderView])
async def connections(admin: Admin, db: Database):
    rows = await db.scalars(
        select(Integration)
        .where(Integration.kind.in_(downloaders.TRANSFER_KINDS), Integration.owner_id.is_(None))
        .order_by(Integration.name, Integration.id)
    )
    sources = await import_sources(db)
    return [view(row, sources) for row in rows]


class DownloadFolderView(BaseModel):
    path: str | None
    parent: str | None
    directories: list[str]
    truncated: bool


@router.get("/folders", response_model=DownloadFolderView)
async def folders(
    admin: Admin, db: Database, path: str | None = Query(default=None, max_length=2000)
):
    return await run_in_threadpool(browse_folders, await storage_settings(db), path)


async def remember_sources(db, declared, retired):
    if not declared and not retired:
        return
    storage = await db.get(ImportStorageSettings, 1, with_for_update=True)
    if not storage:
        storage = ImportStorageSettings(id=1, destinations={}, sources={})
        db.add(storage)
        await db.flush()
    sources = {**storage.sources, **declared}
    for key in retired:
        sources.pop(key, None)
    storage.sources = sources


def retired_keys(previous, bound, env_sources, others):
    kept = {item["source_key"] for item in bound}
    used_elsewhere = {
        mapping["source_key"]
        for row in others
        for mapping in (row.config or {}).get("mappings", [])
    }
    return [
        mapping["source_key"]
        for mapping in previous
        if mapping["source_key"] not in kept
        and mapping["source_key"] not in used_elsewhere
        and mapping["source_key"] not in env_sources
    ]


async def save(body, admin, db, connection_id=None):
    await transaction_lock(db, downloaders.SETTINGS_LOCK)
    await check_actor(db, admin.id, admin=True)
    row = await downloaders.connection_or_404(db, connection_id) if connection_id else None
    if (row.credential_generation if row else 0) != body.expected_generation:
        raise HTTPException(409, "Downloader settings changed. Reload before saving.")
    if row and row.kind != body.kind:
        raise HTTPException(409, "Downloader type cannot be changed")
    duplicate = await db.scalar(
        select(Integration).where(
            Integration.kind == body.kind, Integration.base_url == body.base_url
        )
    )
    if duplicate and (not row or duplicate.id != row.id):
        raise HTTPException(409, "This downloader endpoint already has a connection")
    mounted = await import_sources(db)
    if body.mappings is not None:
        save_path = body.save_path or ((row.config or {}).get("save_path") if row else "")
        if not save_path:
            raise HTTPException(422, "Test the downloader before mapping its folder")
        mappings, declared = (
            downloaders.bind_mappings(body.mappings, save_path, mounted)
            if body.mappings
            else ([], {})
        )
        settings = await storage_settings(db)
        for path in declared.values():
            if downloaders.library_conflict(Path(path), settings):
                raise HTTPException(
                    422,
                    "Download folders must be separate from library and staging folders",
                )
        previous = (row.config or {}).get("mappings", []) if row else []
        others_query = select(Integration).where(
            Integration.kind.in_(downloaders.TRANSFER_KINDS), Integration.owner_id.is_(None)
        )
        if row:
            others_query = others_query.where(Integration.id != row.id)
        others = list(await db.scalars(others_query))
        await remember_sources(
            db, declared, retired_keys(previous, mappings, get_settings().import_sources, others)
        )
    else:
        mappings = row.config.get("mappings", []) if row and row.base_url == body.base_url else []
    same_endpoint = bool(row and row.base_url == body.base_url)
    if body.kind == "sabnzbd":
        secrets = decrypt_secrets(row.encrypted_secrets) if same_endpoint else {"api_key": ""}
        if body.api_key:
            secrets = {"api_key": body.api_key.get_secret_value()}
        if not secrets.get("api_key"):
            raise HTTPException(422, "Enter an API key when connecting SABnzbd")
    else:
        secrets = (
            decrypt_secrets(row.encrypted_secrets)
            if same_endpoint
            else {"username": "", "password": ""}
        )
        if body.username is not None or body.password is not None:
            secrets = {
                "username": body.username.get_secret_value() if body.username else "",
                "password": body.password.get_secret_value() if body.password else "",
            }
    if not row:
        row = Integration(kind=body.kind, credential_generation=0)
        db.add(row)
    row.name, row.base_url, row.enabled = body.name, body.base_url, body.enabled
    row.encrypted_secrets = encrypt_secrets(secrets)
    previous_config = row.config or {}
    same_folder = same_endpoint and previous_config.get("category") == body.category
    row.config = {
        "save_path": body.save_path
        or (previous_config.get("save_path", "") if same_folder else ""),
        "category": body.category,
        "mappings": mappings,
        "client_managed": body.save_path is None,
    }
    row.credential_generation += 1
    row.capabilities = {}
    row.status, row.last_error, row.last_success_at = "untested", None, None
    # Preserve active diagnostic leases and cooldowns across configuration edits.
    await db.flush()
    db.add(AuditEvent(actor_id=admin.id, action="downloader.saved", entity_id=row.id))
    await db.commit()
    return view(row, await import_sources(db))


@router.post("", response_model=DownloaderView, status_code=201)
async def create_connection(body: DownloaderInput, admin: Admin, db: Database):
    return await save(body, admin, db)


@router.put("/{connection_id}", response_model=DownloaderView)
async def update_connection(connection_id: UUID, body: DownloaderInput, admin: Admin, db: Database):
    return await save(body, admin, db, connection_id)


@router.post("/{connection_id}/test", response_model=DownloaderView)
async def test_connection(connection_id: UUID, admin: Admin, db: Database):
    user_id = admin.id
    await db.rollback()
    try:
        row = await downloaders.transfer_connection(db, connection_id)
        kind = row.kind
        await db.rollback()
        if kind == "slskd":
            from app.domain import slskd_connection

            await slskd_connection.test_connection(user_id)
        else:
            await downloaders.test_connection(user_id, connection_id)
    except AdapterError as error:
        raise adapter_http_error(error) from error
    row = await downloaders.transfer_connection(db, connection_id)
    return view(row, await import_sources(db))


@router.post("/{connection_id}/preview-path", response_model=PathPreviewView)
async def preview_path(connection_id: UUID, body: PathPreviewInput, admin: Admin, db: Database):
    row = await downloaders.transfer_connection(db, connection_id)
    if row.credential_generation != body.expected_generation:
        raise HTTPException(409, "Downloader settings changed. Reload before previewing.")
    return PathPreviewView(**downloaders.mapped_path(row, body.path, await import_sources(db)))


class MappingInput(BaseModel):
    expected_generation: int = Field(ge=1)
    mappings: list[DownloadMapping] = Field(max_length=20)


@router.put("/{connection_id}/mappings", response_model=DownloaderView)
async def update_mappings(connection_id: UUID, body: MappingInput, admin: Admin, db: Database):
    await transaction_lock(db, downloaders.SETTINGS_LOCK)
    await check_actor(db, admin.id, admin=True)
    row = await downloaders.transfer_connection(db, connection_id)
    if row.credential_generation != body.expected_generation:
        raise HTTPException(409, "Downloader settings changed. Reload before saving.")
    if not row.config.get("save_path"):
        raise HTTPException(422, "Test the downloader before mapping its folder")
    settings = await storage_settings(db)
    mappings, declared = (
        downloaders.bind_mappings(body.mappings, row.config["save_path"], settings.import_sources)
        if body.mappings
        else ([], {})
    )
    for path in declared.values():
        if downloaders.library_conflict(Path(path), settings):
            raise HTTPException(
                422, "Download folders must be separate from library and staging folders"
            )
    others = list(
        await db.scalars(
            select(Integration).where(
                Integration.kind.in_(downloaders.TRANSFER_KINDS),
                Integration.owner_id.is_(None),
                Integration.id != row.id,
            )
        )
    )
    await remember_sources(
        db,
        declared,
        retired_keys(
            row.config.get("mappings", []), mappings, get_settings().import_sources, others
        ),
    )
    row.config = {**row.config, "mappings": mappings}
    row.credential_generation += 1
    db.add(AuditEvent(actor_id=admin.id, action="downloader.mappings.updated", entity_id=row.id))
    await db.commit()
    return view(row, await import_sources(db))
