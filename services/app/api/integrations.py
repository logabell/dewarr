from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import select, update

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.contracts import AdapterError
from app.adapters.grimmory import Grimmory
from app.adapters.http import configured_url
from app.api.dependencies import Admin, Database
from app.api.operations import OperationView
from app.config import get_settings
from app.db.models import AuditEvent, Integration, Library
from app.domain.operations import enqueue_sync
from app.security import decrypt_secrets, encrypt_secrets

router = APIRouter(prefix="/integrations", tags=["integrations"])


class ABSConnectionInput(BaseModel):
    kind: Literal["audiobookshelf", "grimmory"] = "audiobookshelf"
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    base_url: str = Field(max_length=2000)
    public_url: str | None = Field(default=None, max_length=2000)
    token: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    username: SecretStr | None = Field(default=None, min_length=1, max_length=200)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    enabled: bool = True

    @field_validator("base_url", "public_url")
    @classmethod
    def validate_endpoint(cls, value):
        return configured_url(value) if value is not None else None


class ConnectionView(BaseModel):
    id: UUID
    kind: str
    name: str
    base_url: str
    public_url: str
    enabled: bool
    status: str
    has_token: bool
    version: str | None
    scan_supported: bool
    last_error: str | None
    last_success_at: datetime | None
    library_count: int | None = None
    book_count: int | None = None


def connection_view(value: Integration) -> ConnectionView:
    return ConnectionView(
        id=value.id,
        kind=value.kind,
        name=value.name,
        base_url=value.base_url,
        public_url=value.config.get("public_url") or value.base_url,
        enabled=value.enabled,
        status=value.status,
        has_token=bool(value.encrypted_secrets),
        version=value.capabilities.get("version"),
        scan_supported="scan" in value.capabilities.get("operations", []),
        last_error=value.last_error,
        last_success_at=value.last_success_at,
        library_count=value.capabilities.get("library_count"),
        book_count=value.capabilities.get("book_count"),
    )


async def connection_or_404(db, identifier):
    value = await db.get(Integration, identifier)
    if (
        not value
        or value.deleted_at
        or value.owner_id
        or value.kind not in {"audiobookshelf", "grimmory"}
    ):
        raise HTTPException(404, "Connection not found")
    return value


@router.get("", response_model=list[ConnectionView])
async def connections(admin: Admin, db: Database):
    records = (
        await db.scalars(
            select(Integration)
            .where(
                Integration.owner_id.is_(None),
                Integration.deleted_at.is_(None),
                Integration.kind.in_(["audiobookshelf", "grimmory"]),
            )
            .order_by(Integration.name)
        )
    ).all()
    return [connection_view(record) for record in records]


class ConnectionCheck(BaseModel):
    library_count: int
    book_count: int
    version: str | None


def connection_secrets(body: ABSConnectionInput, saved: dict | None = None) -> dict:
    saved = saved or {}
    if body.kind == "grimmory":
        if body.token:
            raise HTTPException(422, "Grimmory uses a username and password")
        username = body.username.get_secret_value() if body.username else saved.get("username")
        password = body.password.get_secret_value() if body.password else saved.get("password")
        if not username or not password:
            raise HTTPException(422, "Enter the Grimmory username and password")
        return {"username": username, "password": password}
    if body.username or body.password:
        raise HTTPException(422, "Audiobookshelf uses an API token")
    token = body.token.get_secret_value() if body.token else saved.get("token")
    if not token:
        raise HTTPException(422, "Enter an Audiobookshelf API token")
    return {"token": token}


async def inspect_connection(kind: str, endpoint: str, secrets: dict):
    client_type = Grimmory if kind == "grimmory" else Audiobookshelf
    credential = secrets if kind == "grimmory" else secrets["token"]
    try:
        async with client_type(endpoint, credential) as client:
            capabilities, _ = await client.authorize()
            libraries = await client.libraries()
            total = 0
            for library in libraries:
                _, count = await client.page(library["id"], 0)
                total += count
        return {
            **capabilities.model_dump(mode="json"),
            "library_count": len(libraries),
            "book_count": total,
        }
    except AdapterError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/check", response_model=ConnectionCheck)
async def check_connection(
    body: ABSConnectionInput, admin: Admin, db: Database, integration_id: UUID | None = None
):
    saved = None
    if integration_id:
        record = await connection_or_404(db, integration_id)
        if record.kind != body.kind:
            raise HTTPException(422, "A connection cannot change library apps")
        saved = decrypt_secrets(record.encrypted_secrets)
    return await inspect_connection(body.kind, body.base_url, connection_secrets(body, saved))


@router.post("", response_model=ConnectionView, status_code=201)
async def create_connection(body: ABSConnectionInput, admin: Admin, db: Database):
    secrets = connection_secrets(body)
    capabilities = await inspect_connection(body.kind, body.base_url, secrets)
    record = Integration(
        kind=body.kind,
        name=body.name.strip(),
        base_url=body.base_url,
        config={"public_url": body.public_url or body.base_url, "created_by": str(admin.id)},
        encrypted_secrets=encrypt_secrets(secrets),
        enabled=body.enabled,
        status="connected",
        capabilities=capabilities,
    )
    db.add(record)
    await db.flush()
    db.add(AuditEvent(actor_id=admin.id, action="integration.created", entity_id=record.id))
    await db.commit()
    return connection_view(record)


@router.put("/{integration_id}", response_model=ConnectionView)
async def update_connection(
    integration_id: UUID, body: ABSConnectionInput, admin: Admin, db: Database
):
    record = await connection_or_404(db, integration_id)
    if record.kind != body.kind:
        raise HTTPException(422, "A connection cannot change library apps")
    generation = record.credential_generation
    secrets = connection_secrets(body, decrypt_secrets(record.encrypted_secrets))
    capabilities = await inspect_connection(body.kind, body.base_url, secrets)
    await db.refresh(record, with_for_update=True)
    if record.deleted_at or record.credential_generation != generation:
        raise HTTPException(409, "Connection changed while checking. Try again.")
    record.name, record.base_url, record.enabled = body.name.strip(), body.base_url, body.enabled
    record.config = {**record.config, "public_url": body.public_url or body.base_url}
    if body.token or body.username or body.password:
        record.encrypted_secrets = encrypt_secrets(secrets)
    record.credential_generation += 1
    record.lease_token, record.lease_until = None, None
    record.status, record.last_error, record.next_sync_at = "connected", None, None
    record.capabilities = capabilities
    await db.execute(
        update(Library).where(Library.integration_id == record.id).values(accessible=False)
    )
    db.add(AuditEvent(actor_id=admin.id, action="integration.updated", entity_id=record.id))
    await db.commit()
    return connection_view(record)


@router.post("/{integration_id}/test", response_model=ConnectionView)
async def test_connection(integration_id: UUID, admin: Admin, db: Database):
    record = await connection_or_404(db, integration_id)
    if not record.enabled:
        raise HTTPException(409, "Enable this connection before testing it")
    generation, endpoint, kind = record.credential_generation, record.base_url, record.kind
    secrets = decrypt_secrets(record.encrypted_secrets)
    credential = secrets if kind == "grimmory" else secrets["token"]
    client_type = Grimmory if kind == "grimmory" else Audiobookshelf
    await db.rollback()
    try:
        async with client_type(endpoint, credential) as client:
            capabilities, _ = await client.authorize()
            libraries = await client.libraries()
            book_count = 0
            for library in libraries:
                _, count = await client.page(library["id"], 0)
                book_count += count
        status, message = "connected", None
    except AdapterError as error:
        capabilities, status, message = None, error.kind.value, str(error)
    record = await connection_or_404(db, integration_id)
    await db.refresh(record, with_for_update=True)
    if record.deleted_at or record.credential_generation != generation:
        raise HTTPException(409, "Connection settings changed while the test was running")
    record.status, record.last_error = status, message
    if capabilities:
        record.capabilities = {
            **capabilities.model_dump(mode="json"),
            "library_count": len(libraries),
            "book_count": book_count,
        }
    await db.commit()
    return connection_view(record)


@router.post("/{integration_id}/sync", response_model=OperationView, status_code=202)
async def sync_connection(
    integration_id: UUID,
    admin: Admin,
    db: Database,
    idempotency_key: str = Header(min_length=8, max_length=200),
):
    record = await connection_or_404(db, integration_id)
    if not record.enabled or get_settings().recovery_mode:
        raise HTTPException(409, "Sync is paused or this connection is disabled")
    operation = await enqueue_sync(db, admin.id, record.id, idempotency_key)
    record.next_sync_at = datetime.now(UTC)
    await db.commit()
    return operation
