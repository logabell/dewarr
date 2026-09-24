import asyncio
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.http import configured_url
from app.adapters.mam import (
    AccountAutomation,
    MAMRelease,
    MAMSearch,
    ReleasePage,
    cookie_value,
    stored_automation,
)
from app.api.dependencies import Admin, CurrentUser, Database
from app.api.metadata import adapter_http_error
from app.db.models import AuditEvent, SourceConnection
from app.domain.mam_diagnostics import EgressResult, probe_egress
from app.domain.operations import transaction_lock
from app.domain.source_network import source_call
from app.security import decrypt_secrets, encrypt_secrets

router = APIRouter(prefix="/sources/mam", tags=["sources"])


class MAMConnectionInput(BaseModel):
    base_url: str = Field(default="https://www.myanonamouse.net", max_length=2000)
    proxy_url: str | None = Field(default=None, max_length=2000)
    proxy_fallback_direct: bool = True
    mam_id: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    proxy_username: SecretStr | None = Field(default=None, min_length=1, max_length=300)
    proxy_password: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    clear_proxy_credentials: bool = False
    enabled: bool = True
    automation: AccountAutomation = Field(default_factory=AccountAutomation)
    expected_generation: int = Field(default=0, ge=0)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        return configured_url(value)

    @field_validator("proxy_url")
    @classmethod
    def proxy_endpoint(cls, value):
        return configured_url(value) if value else None

    @field_validator("mam_id")
    @classmethod
    def cookie(cls, value):
        return SecretStr(cookie_value(value.get_secret_value())) if value else None

    @model_validator(mode="after")
    def proxy(self):
        if self.proxy_url and urlsplit(self.proxy_url).path:
            raise ValueError("Use an HTTP(S) proxy origin without a path")
        if bool(self.proxy_username) != bool(self.proxy_password):
            raise ValueError("Enter both proxy username and password")
        if (self.proxy_username or self.proxy_password) and not self.proxy_url:
            raise ValueError("Configure the proxy URL before its credentials")
        if self.clear_proxy_credentials and self.proxy_username:
            raise ValueError("Choose replacement proxy credentials or clearing, not both")
        return self


class MAMConnectionView(BaseModel):
    configured: bool
    enabled: bool
    base_url: str
    proxy_url: str | None
    proxy_fallback_direct: bool
    has_session: bool
    has_proxy_credentials: bool
    generation: int
    status: str
    last_error: str | None
    last_success_at: datetime | None
    route: str
    automation: AccountAutomation = Field(default_factory=AccountAutomation)


def view(row):
    secrets = decrypt_secrets(row.encrypted_secrets) if row else {}
    return MAMConnectionView(
        configured=bool(row and not row.deleted_at),
        enabled=bool(row and row.enabled),
        base_url=row.base_url if row and not row.deleted_at else "https://www.myanonamouse.net",
        proxy_url=row.proxy_url if row else None,
        proxy_fallback_direct=row.proxy_fallback_direct if row else True,
        has_session=bool(secrets.get("mam_id")),
        has_proxy_credentials=bool(secrets.get("proxy_password")),
        generation=row.generation if row else 0,
        status=row.status if row else "not-configured",
        last_error=row.last_error if row else None,
        last_success_at=row.last_success_at if row else None,
        route=(
            "proxy-preferred"
            if row and row.proxy_url and row.proxy_fallback_direct
            else "required-proxy"
            if row and row.proxy_url
            else "direct"
        ),
        automation=stored_automation(row.automation) if row else AccountAutomation(),
    )


@router.get("/connection", response_model=MAMConnectionView)
async def connection(admin: Admin, db: Database):
    return view(await db.get(SourceConnection, "mam"))


@router.put("/connection", response_model=MAMConnectionView)
async def save_connection(body: MAMConnectionInput, admin: Admin, db: Database):
    await transaction_lock(db, "source:mam")
    row = await db.get(SourceConnection, "mam")
    if (row.generation if row else 0) != body.expected_generation:
        raise HTTPException(409, "MAM settings changed. Reload before saving.")
    secrets = decrypt_secrets(row.encrypted_secrets) if row else {}
    if row and row.base_url != body.base_url and secrets.get("mam_id") and not body.mam_id:
        raise HTTPException(422, "Enter mam_id when connecting a new MAM endpoint")
    if not row:
        row = SourceConnection(key="mam", generation=0)
        db.add(row)
    if body.mam_id:
        secrets["mam_id"] = body.mam_id.get_secret_value()
        if row.lease_token and row.lease_until and row.lease_until <= datetime.now(UTC):
            row.lease_token, row.lease_until = None, None
    if body.clear_proxy_credentials or row.proxy_url != body.proxy_url:
        secrets.pop("proxy_username", None)
        secrets.pop("proxy_password", None)
    if body.proxy_username and body.proxy_password:
        secrets.update(
            proxy_username=body.proxy_username.get_secret_value(),
            proxy_password=body.proxy_password.get_secret_value(),
        )
    row.base_url, row.proxy_url, row.enabled = body.base_url, body.proxy_url, body.enabled
    row.proxy_fallback_direct = body.proxy_fallback_direct
    row.automation = body.automation.model_dump()
    row.encrypted_secrets = encrypt_secrets(secrets)
    row.deleted_at = None
    row.generation += 1
    row.status, row.last_error, row.last_success_at = "untested", None, None
    # Source-imposed cooldown survives configuration edits and process restarts.
    db.add(AuditEvent(actor_id=admin.id, action="source.mam.updated"))
    await db.commit()
    return view(row)


async def call(user_id, operation, argument=None):
    try:
        return await source_call(user_id, operation, argument)
    except AdapterError as error:
        raise adapter_http_error(error) from error


@router.post("/connection/test", response_model=MAMConnectionView)
async def test_connection(admin: Admin, db: Database):
    user_id = admin.id
    await db.rollback()
    await call(user_id, "test")
    return view(await db.get(SourceConnection, "mam", populate_existing=True))


@router.post("/search", response_model=ReleasePage)
async def search(body: MAMSearch, user: CurrentUser, db: Database):
    user_id = user.id
    await db.rollback()
    return await call(user_id, "search", body)


@router.get("/releases/{source_id}", response_model=MAMRelease)
async def detail(
    user: CurrentUser, db: Database, source_id: str = Path(pattern=r"^[1-9][0-9]{0,17}$")
):
    user_id = user.id
    await db.rollback()
    return await call(user_id, "detail", source_id)


class MAMNetworkView(BaseModel):
    connection: MAMConnectionView
    checked_at: datetime
    status: str
    route: Literal["direct", "proxy", "direct-fallback"]
    cookie_status: str
    proxy_status: str
    proxy: EgressResult | None
    direct: EgressResult
    message: str


@router.post("/network/test", response_model=MAMNetworkView)
async def test_network(admin: Admin, db: Database):
    row = await db.get(SourceConnection, "mam")
    if not row or not row.enabled:
        raise HTTPException(409, "Connect and enable MAM before testing the network")
    generation, proxy_url = row.generation, row.proxy_url
    secrets = decrypt_secrets(row.encrypted_secrets)
    user_id = admin.id
    await db.rollback()

    async def test_cookie():
        if not secrets.get("mam_id"):
            return None, None
        try:
            _, route = await source_call(
                user_id,
                "test",
                expected_generation=generation,
                with_route=True,
            )
            return None, route
        except AdapterError as error:
            return error, None

    async def test_proxy():
        if not proxy_url:
            return None
        return await probe_egress(
            proxy_url, secrets.get("proxy_username"), secrets.get("proxy_password")
        )

    cookie_result, proxy, direct = await asyncio.gather(test_cookie(), test_proxy(), probe_egress())
    failure, used_route = cookie_result
    row = await db.get(SourceConnection, "mam", populate_existing=True)
    if not row or row.generation != generation or not row.enabled:
        raise HTTPException(409, "MAM settings changed during the network test. Test again.")
    has_session = bool(secrets.get("mam_id"))
    authenticated = has_session and failure is None
    route = used_route or ("proxy" if proxy_url else "direct")
    healthy = (
        authenticated
        and route != "direct-fallback"
        and bool(direct.ip)
        and (proxy is None or bool(proxy.ip))
    )
    return MAMNetworkView(
        connection=view(row),
        checked_at=datetime.now(UTC),
        status="healthy"
        if healthy
        else "degraded"
        if authenticated or (not has_session and direct.ip and (proxy is None or proxy.ip))
        else "unhealthy",
        route=route,
        cookie_status="not-configured"
        if not has_session
        else "authenticated"
        if authenticated
        else ("rejected" if failure.kind == FailureKind.AUTHENTICATION else "unverified"),
        proxy_status="not-configured"
        if proxy is None
        else ("healthy" if proxy.ip else "unavailable"),
        proxy=proxy,
        direct=direct,
        message=(
            "Network checks completed without a MAM cookie. Enter mam_id to verify MAM access."
            if not has_session
            else "The configured MAM proxy could not be reached. MAM authenticated through "
            "the direct fallback route."
            if route == "direct-fallback"
            else str(failure)
            if failure
            else (
                "MAM authenticated through the configured proxy."
                if proxy_url
                else "MAM authenticated through the direct connection."
            )
        ),
    )
