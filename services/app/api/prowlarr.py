from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import delete, select

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.http import configured_url
from app.adapters.prowlarr import ProwlarrIndexer, ProwlarrRelease, ProwlarrSearch
from app.api.dependencies import Admin, CurrentUser, Database, Member
from app.api.metadata import adapter_http_error
from app.api.source_artifacts import SourceArtifactView, artifact_view
from app.db.models import AuditEvent, SourceConnection, SourceResult
from app.domain.operations import transaction_lock
from app.domain.prowlarr_network import prowlarr_call
from app.domain.source_artifacts import persist_artifact
from app.domain.source_network import check_actor
from app.security import decrypt_secrets, encrypt_secrets

router = APIRouter(prefix="/sources/prowlarr", tags=["sources"])


class ProwlarrConnectionInput(BaseModel):
    base_url: str = Field(max_length=2000)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=1000)
    enabled: bool = True
    excluded_indexers: list[int] = Field(default_factory=list, max_length=1000)
    expected_generation: int = Field(default=0, ge=0)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        return configured_url(value)

    @field_validator("api_key")
    @classmethod
    def token(cls, value):
        if value and any(ord(c) < 33 or ord(c) > 126 for c in value.get_secret_value()):
            raise ValueError("Enter a valid API key without whitespace")
        return value

    @field_validator("excluded_indexers")
    @classmethod
    def indexer_ids(cls, value):
        if any(i < 1 for i in value):
            raise ValueError("Indexer IDs must be positive")
        return sorted(set(value))


class ProwlarrConnectionView(BaseModel):
    configured: bool
    base_url: str
    has_api_key: bool
    enabled: bool
    generation: int
    excluded_indexers: list[int]
    status: str
    last_error: str | None
    last_success_at: datetime | None


def view(row):
    secrets = decrypt_secrets(row.encrypted_secrets) if row else {}
    return ProwlarrConnectionView(
        configured=bool(row and not row.deleted_at),
        base_url=row.base_url if row else "",
        has_api_key=bool(secrets.get("api_key")),
        enabled=bool(row and row.enabled),
        generation=row.generation if row else 0,
        excluded_indexers=secrets.get("excluded_indexers", []),
        status=row.status if row else "not-configured",
        last_error=row.last_error if row else None,
        last_success_at=row.last_success_at if row else None,
    )


@router.get("/connection", response_model=ProwlarrConnectionView)
async def connection(admin: Admin, db: Database):
    return view(await db.get(SourceConnection, "prowlarr"))


@router.put("/connection", response_model=ProwlarrConnectionView)
async def save_connection(body: ProwlarrConnectionInput, admin: Admin, db: Database):
    await transaction_lock(db, "source:prowlarr")
    row = await db.get(SourceConnection, "prowlarr")
    if (row.generation if row else 0) != body.expected_generation:
        raise HTTPException(409, "Prowlarr settings changed. Reload before saving.")
    secrets = decrypt_secrets(row.encrypted_secrets) if row else {}
    if (not row or row.base_url != body.base_url) and not body.api_key:
        raise HTTPException(422, "Enter an API key when connecting a new Prowlarr endpoint")
    if not row:
        row = SourceConnection(key="prowlarr", generation=0)
        db.add(row)
    if body.api_key:
        secrets["api_key"] = body.api_key.get_secret_value()
    secrets["excluded_indexers"] = body.excluded_indexers
    row.base_url, row.enabled = body.base_url, body.enabled
    row.encrypted_secrets = encrypt_secrets(secrets)
    row.deleted_at = None
    row.generation += 1
    row.status, row.last_error, row.last_success_at = "untested", None, None
    db.add(AuditEvent(actor_id=admin.id, action="source.prowlarr.updated"))
    await db.commit()
    return view(row)


async def call(*args, **kwargs):
    try:
        return await prowlarr_call(*args, **kwargs)
    except AdapterError as error:
        if error.kind == FailureKind.UNSUPPORTED:
            raise HTTPException(422, str(error)) from error
        raise adapter_http_error(error) from error


@router.post("/connection/test", response_model=ProwlarrConnectionView)
async def test_connection(admin: Admin, db: Database):
    user_id = admin.id
    await db.rollback()
    await call(user_id, "test")
    return view(await db.get(SourceConnection, "prowlarr", populate_existing=True))


@router.get("/indexers", response_model=list[ProwlarrIndexer])
async def indexers(user: CurrentUser, db: Database):
    user_id = user.id
    await db.rollback()
    values, _ = await call(user_id, "indexers")
    return values


class ProwlarrResultView(BaseModel):
    id: UUID
    expires_at: datetime
    release: ProwlarrRelease


class ProwlarrPage(BaseModel):
    items: list[ProwlarrResultView]
    offset: int
    limit: int
    may_have_more: bool
    warnings: list[str]


@router.post("/search", response_model=ProwlarrPage)
async def search(body: ProwlarrSearch, user: CurrentUser, db: Database):
    user_id = user.id
    await db.rollback()
    batch, generation = await call(user_id, "search", body)
    await transaction_lock(db, "source:prowlarr")
    await check_actor(db, user_id)
    source = await db.get(SourceConnection, "prowlarr", populate_existing=True)
    if not source or not source.enabled or source.generation != generation:
        raise HTTPException(409, "Prowlarr settings changed. Search again.")
    now = datetime.now(UTC)
    await db.execute(delete(SourceResult).where(SourceResult.expires_at <= now))
    # Keep temporary observations bounded per account, independently of durable artifacts.
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
    for hit in batch.hits:
        row = SourceResult(
            owner_id=user_id,
            source_key="prowlarr",
            source_generation=generation,
            expires_at=now + timedelta(minutes=25),
            encrypted_reference=encrypt_secrets({"link": hit.reference}),
            release_snapshot=hit.release.model_dump(mode="json"),
        )
        db.add(row)
        await db.flush()
        items.append(ProwlarrResultView(id=row.id, expires_at=row.expires_at, release=hit.release))
    await db.commit()
    return ProwlarrPage(
        items=items,
        offset=body.offset,
        limit=body.limit,
        may_have_more=batch.returned_count >= body.limit,
        warnings=[
            "Prowlarr may return an empty result when an upstream search fails. "
            "Check Prowlarr diagnostics before treating it as absence."
        ],
    )


@router.post("/results/{result_id}/artifact", response_model=SourceArtifactView)
async def resolve(result_id: UUID, user: Member, db: Database):
    row = await db.get(SourceResult, result_id)
    if not row or row.owner_id != user.id:
        raise HTTPException(404, "Source result not found")
    if row.expires_at <= datetime.now(UTC):
        raise HTTPException(409, "This source result expired. Search again.")
    release = ProwlarrRelease.model_validate(row.release_snapshot)
    reference = decrypt_secrets(row.encrypted_reference).get("link")
    if not reference or not release.acquisition_supported:
        label = "NZB" if release.protocol == "nzb" else "torrent"
        raise HTTPException(422, f"This result has no supported {label} file")
    user_id, generation = user.id, row.source_generation
    await db.rollback()
    artifact, _ = await call(
        user_id, "resolve", (release, reference), expected_generation=generation
    )
    try:
        identifier = await persist_artifact(
            user_id, release.source_id, artifact, generation, "prowlarr"
        )
    except AdapterError as error:
        if error.kind == FailureKind.UNSUPPORTED:
            raise HTTPException(422, str(error)) from error
        raise adapter_http_error(error) from error
    return await artifact_view(db, identifier, user_id)
