from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.adapters.contracts import AdapterError
from app.adapters.http import configured_url
from app.api.dependencies import Admin, Database
from app.api.metadata import adapter_http_error
from app.db.models import Integration, SourceConnection
from app.domain import slskd_connection
from app.domain.downloaders import mappings_current
from app.importing.storage import import_sources

router = APIRouter(prefix="/sources/slskd", tags=["sources"])


class SlskdConnectionInput(BaseModel):
    base_url: str = Field(default="http://127.0.0.1:5030", max_length=2000)
    api_key: SecretStr | None = Field(default=None, min_length=16, max_length=255)
    enabled: bool = True
    expected_generation: int = Field(default=0, ge=0)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        return configured_url(value)

    @field_validator("api_key")
    @classmethod
    def key(cls, value):
        if value is None:
            return None
        text = value.get_secret_value().strip()
        if len(text) < 16 or any(character.isspace() for character in text):
            raise ValueError("Use the slskd API key with no spaces")
        return SecretStr(text)


class SlskdConnectionView(BaseModel):
    configured: bool
    enabled: bool
    base_url: str
    has_api_key: bool
    generation: int
    status: str
    last_error: str | None
    last_success_at: datetime | None
    download_root: str | None
    mapped: bool
    downloader_id: UUID | None
    downloader_generation: int


def view(
    source: SourceConnection | None, client: Integration | None, sources
) -> SlskdConnectionView:
    secrets = {}
    if source:
        from app.security import decrypt_secrets

        secrets = decrypt_secrets(source.encrypted_secrets)
    root = (client.config.get("save_path") if client else "") or ""
    mapped = bool(client and mappings_current(client, sources))
    return SlskdConnectionView(
        configured=bool(source and not source.deleted_at),
        enabled=bool(source and source.enabled),
        base_url=source.base_url if source and not source.deleted_at else "http://127.0.0.1:5030",
        has_api_key=bool(secrets.get("api_key")),
        generation=source.generation if source else 0,
        status=source.status if source else "not-configured",
        last_error=source.last_error if source else None,
        last_success_at=source.last_success_at if source else None,
        download_root=root or None,
        mapped=mapped,
        downloader_id=client.id if client else None,
        downloader_generation=client.credential_generation if client else 0,
    )


@router.get("/connection", response_model=SlskdConnectionView)
async def connection(admin: Admin, db: Database):
    return view(
        await db.get(SourceConnection, "slskd"),
        await slskd_connection.integration(db),
        await import_sources(db),
    )


@router.put("/connection", response_model=SlskdConnectionView)
async def save_connection(body: SlskdConnectionInput, admin: Admin, db: Database):
    source, client = await slskd_connection.save(db, admin, body)
    await db.commit()
    return view(source, client, await import_sources(db))


@router.post("/connection/test", response_model=SlskdConnectionView)
async def test_connection(admin: Admin, db: Database):
    user_id = admin.id
    await db.rollback()
    try:
        await slskd_connection.test_connection(user_id)
    except AdapterError as error:
        raise adapter_http_error(error) from error
    return view(
        await db.get(SourceConnection, "slskd", populate_existing=True),
        await slskd_connection.integration(db),
        await import_sources(db),
    )
