from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import delete, select

from app.adapters.audiobookbay import ABBRelease, ABBSearch, endpoint
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.http import configured_url
from app.api.dependencies import Admin, CurrentUser, Database, Member
from app.api.metadata import adapter_http_error
from app.api.source_artifacts import SourceArtifactView, artifact_view
from app.db.models import AuditEvent, SourceConnection, SourceResult
from app.domain.audiobookbay_network import abb_call, resolve_abb
from app.domain.connection_health import connection_status
from app.domain.downloaders import SETTINGS_LOCK, connection_or_404
from app.domain.operations import transaction_lock
from app.domain.source_network import check_actor
from app.security import decrypt_secrets, encrypt_secrets

router = APIRouter(prefix="/sources/audiobookbay", tags=["sources"])


class ABBConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=2000)
    proxy_url: str | None = Field(default=None, max_length=2000)
    proxy_username: SecretStr | None = Field(default=None, min_length=1, max_length=300)
    proxy_password: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    clear_proxy_credentials: bool = False
    metadata_downloader_id: UUID | None = None
    enabled: bool = True
    expected_generation: int = Field(default=0, ge=0)

    @field_validator("base_url")
    @classmethod
    def origin(cls, value):
        return endpoint(value)

    @field_validator("proxy_url")
    @classmethod
    def proxy_endpoint(cls, value):
        return configured_url(value) if value else None

    @model_validator(mode="after")
    def proxy(self):
        if self.proxy_url and urlsplit(self.proxy_url).path:
            raise ValueError("Use a proxy origin without a path")
        if bool(self.proxy_username) != bool(self.proxy_password):
            raise ValueError("Enter both proxy username and password")
        if self.proxy_username and not self.proxy_url:
            raise ValueError("Configure the proxy URL before its credentials")
        if self.clear_proxy_credentials and self.proxy_username:
            raise ValueError("Choose replacement proxy credentials or clearing, not both")
        return self


class ABBConnectionView(BaseModel):
    configured: bool
    base_url: str
    proxy_url: str | None
    has_proxy_credentials: bool
    metadata_downloader_id: UUID | None
    enabled: bool
    generation: int
    status: str
    last_error: str | None
    last_success_at: datetime | None
    route: str


def view(row):
    secrets = decrypt_secrets(row.encrypted_secrets) if row else {}
    return ABBConnectionView(
        configured=bool(row and not row.deleted_at),
        base_url=row.base_url if row else "",
        proxy_url=row.proxy_url if row else None,
        has_proxy_credentials=bool(secrets.get("proxy_password")),
        metadata_downloader_id=secrets.get("metadata_downloader_id"),
        enabled=bool(row and row.enabled),
        generation=row.generation if row else 0,
        status=connection_status(row) if row else "not-configured",
        last_error=row.last_error if row else None,
        last_success_at=row.last_success_at if row else None,
        route="required-proxy" if row and row.proxy_url else "direct",
    )


@router.get("/connection", response_model=ABBConnectionView)
async def connection(admin: Admin, db: Database):
    return view(await db.get(SourceConnection, "audiobookbay"))


@router.put("/connection", response_model=ABBConnectionView)
async def save_connection(body: ABBConnectionInput, admin: Admin, db: Database):
    await transaction_lock(db, "source:audiobookbay")
    row = await db.get(SourceConnection, "audiobookbay")
    if (row.generation if row else 0) != body.expected_generation:
        raise HTTPException(409, "AudiobookBay settings changed. Reload before saving.")
    if body.metadata_downloader_id:
        await transaction_lock(db, SETTINGS_LOCK)
        downloader = await connection_or_404(db, body.metadata_downloader_id)
        if downloader.kind != "qbittorrent" or not downloader.enabled:
            raise HTTPException(422, "Choose an enabled qBittorrent metadata downloader")
    secrets = decrypt_secrets(row.encrypted_secrets) if row else {}
    if not row:
        row = SourceConnection(key="audiobookbay", generation=0)
        db.add(row)
    if body.clear_proxy_credentials or row.proxy_url != body.proxy_url:
        secrets.pop("proxy_username", None)
        secrets.pop("proxy_password", None)
    if body.proxy_username and body.proxy_password:
        secrets.update(
            proxy_username=body.proxy_username.get_secret_value(),
            proxy_password=body.proxy_password.get_secret_value(),
        )
    secrets["metadata_downloader_id"] = (
        str(body.metadata_downloader_id) if body.metadata_downloader_id else None
    )
    row.base_url, row.proxy_url, row.enabled = body.base_url, body.proxy_url, body.enabled
    row.encrypted_secrets = encrypt_secrets(secrets)
    row.deleted_at = None
    row.generation += 1
    row.status, row.last_error, row.last_success_at = "untested", None, None
    row.last_checked_at = None
    db.add(AuditEvent(actor_id=admin.id, action="source.audiobookbay.updated"))
    await db.commit()
    return view(row)


async def call(*args, **kwargs):
    try:
        return await abb_call(*args, **kwargs)
    except AdapterError as error:
        raise adapter_http_error(error) from error


@router.post("/connection/test", response_model=ABBConnectionView)
async def test_connection(admin: Admin, db: Database):
    user_id = admin.id
    await db.rollback()
    await call(user_id, "test")
    return view(await db.get(SourceConnection, "audiobookbay", populate_existing=True))


class ABBResultView(BaseModel):
    id: UUID
    expires_at: datetime
    release: ABBRelease


class ABBPageView(BaseModel):
    items: list[ABBResultView]
    page: int
    has_more: bool


@router.post("/search", response_model=ABBPageView)
async def search(body: ABBSearch, user: CurrentUser, db: Database):
    user_id = user.id
    await db.rollback()
    batch, generation = await call(user_id, "search", body)
    await transaction_lock(db, "source:audiobookbay")
    await check_actor(db, user_id)
    source = await db.get(SourceConnection, "audiobookbay", populate_existing=True)
    if not source or not source.enabled or source.generation != generation:
        raise HTTPException(409, "AudiobookBay settings changed. Search again.")
    now = datetime.now(UTC)
    await db.execute(delete(SourceResult).where(SourceResult.expires_at <= now))
    older = list(
        await db.scalars(
            select(SourceResult.id)
            .where(SourceResult.owner_id == user_id)
            .order_by(SourceResult.created_at.desc(), SourceResult.id)
            .offset(2000)
        )
    )
    if older:
        await db.execute(delete(SourceResult).where(SourceResult.id.in_(older)))
    items = []
    for release in batch.items:
        row = SourceResult(
            owner_id=user_id,
            source_key="audiobookbay",
            source_generation=generation,
            expires_at=now + timedelta(minutes=25),
            encrypted_reference=encrypt_secrets({}),
            release_snapshot=release.model_dump(mode="json"),
        )
        db.add(row)
        await db.flush()
        items.append(ABBResultView(id=row.id, expires_at=row.expires_at, release=release))
    await db.commit()
    return ABBPageView(items=items, page=batch.page, has_more=batch.has_more)


async def owned_result(db, user_id, result_id):
    row = await db.get(SourceResult, result_id)
    if not row or row.owner_id != user_id or row.source_key != "audiobookbay":
        raise HTTPException(404, "Source result not found")
    if row.expires_at <= datetime.now(UTC):
        raise HTTPException(409, "Source result expired. Search again.")
    return row


@router.get("/results/{result_id}", response_model=ABBRelease)
async def detail(result_id: UUID, user: CurrentUser, db: Database):
    row = await owned_result(db, user.id, result_id)
    release = ABBRelease.model_validate(row.release_snapshot)
    user_id, generation = user.id, row.source_generation
    await db.rollback()
    result, _ = await call(user_id, "detail", release.detail_path, expected_generation=generation)
    return result.release


@router.post("/results/{result_id}/artifact", response_model=SourceArtifactView)
async def resolve(result_id: UUID, user: Member, db: Database):
    row = await owned_result(db, user.id, result_id)
    release = ABBRelease.model_validate(row.release_snapshot)
    user_id, generation = user.id, row.source_generation
    await db.rollback()
    try:
        identifier, _ = await resolve_abb(user_id, release, expected_generation=generation)
    except AdapterError as error:
        if error.kind == FailureKind.UNSUPPORTED:
            raise HTTPException(422, str(error)) from error
        raise adapter_http_error(error) from error
    return await artifact_view(db, identifier, user_id)
