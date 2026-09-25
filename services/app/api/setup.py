"""Setup readiness and per-user onboarding progress."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.dependencies import Admin, CurrentUser, Database
from app.db.models import (
    CatalogAccount,
    ImportDestination,
    Integration,
    Library,
    SourceConnection,
    User,
)
from app.domain.downloaders import TRANSFER_KINDS, mappings_current
from app.importing.destination_view import view as destination_view
from app.importing.storage import storage_settings

router = APIRouter(prefix="/setup", tags=["setup"])


class SetupService(BaseModel):
    name: str
    enabled: bool
    status: str
    last_success_at: datetime | None


class SetupLibrary(SetupService):
    id: UUID
    libraries: int
    inventoried_libraries: int


class SetupDownloader(SetupService):
    id: UUID
    mappings_current: bool


class SetupSource(SetupService):
    key: str
    uses_proxy: bool


class SetupDestination(BaseModel):
    name: str
    medium: str
    enabled: bool
    configured: bool
    publication_available: bool


class SetupReadiness(BaseModel):
    observed_at: datetime
    download_dispatch_enabled: bool
    libraries: list[SetupLibrary]
    catalog: SetupService | None
    sources: list[SetupSource]
    downloaders: list[SetupDownloader]
    destinations: list[SetupDestination]
    download_roots: int
    destination_roots: int
    staging_configured: bool


def service(row, name):
    return {
        "name": name,
        "enabled": row.enabled,
        "status": row.status,
        "last_success_at": row.last_success_at,
    }


@router.get("/readiness", response_model=SetupReadiness)
async def readiness(admin: Admin, db: Database):
    settings = await storage_settings(db)
    integrations = (
        await db.scalars(
            select(Integration)
            .where(Integration.owner_id.is_(None), Integration.deleted_at.is_(None))
            .order_by(Integration.name, Integration.id)
        )
    ).all()
    libraries = (await db.scalars(select(Library))).all()
    by_integration = {}
    for library in libraries:
        by_integration.setdefault(library.integration_id, []).append(library)
    account = await db.get(CatalogAccount, admin.id)
    destinations = []
    for row in await db.scalars(
        select(ImportDestination)
        .where(ImportDestination.deleted_at.is_(None))
        .order_by(ImportDestination.root_key)
    ):
        current = await destination_view(db, row)
        destinations.append(
            SetupDestination(
                name=current.root_key,
                medium=current.medium,
                enabled=current.enabled,
                configured=current.configured,
                publication_available=current.publication_available,
            )
        )
    source_names = {
        "mam": "MAM",
        "audiobookbay": "AudiobookBay",
        "prowlarr": "Prowlarr",
        "slskd": "Soulseek",
    }
    sources = await db.scalars(
        select(SourceConnection)
        .where(SourceConnection.key.in_(source_names), SourceConnection.deleted_at.is_(None))
        .order_by(SourceConnection.key)
    )
    return SetupReadiness(
        observed_at=datetime.now(UTC),
        download_dispatch_enabled=settings.download_dispatch_enabled,
        libraries=[
            SetupLibrary(
                **service(row, row.name),
                id=row.id,
                libraries=len(by_integration.get(row.id, [])),
                inventoried_libraries=sum(
                    bool(library.accessible and library.last_complete_sync)
                    for library in by_integration.get(row.id, [])
                ),
            )
            for row in integrations
            if row.kind in {"audiobookshelf", "grimmory"}
        ],
        catalog=SetupService(**service(account, "Hardcover")) if account else None,
        sources=[
            SetupSource(
                **service(row, source_names[row.key]),
                key=row.key,
                uses_proxy=bool(row.proxy_url),
            )
            for row in sources
        ],
        downloaders=[
            SetupDownloader(
                id=row.id,
                **service(row, row.name),
                mappings_current=mappings_current(row, settings.import_sources),
            )
            for row in integrations
            if row.kind in TRANSFER_KINDS
        ],
        destinations=destinations,
        download_roots=len(settings.import_sources),
        destination_roots=len(settings.import_destinations),
        staging_configured=bool(settings.import_staging_root or settings.import_storage_routes),
    )


class OnboardingProgress(BaseModel):
    status: Literal["pending", "deferred", "completed", "skipped"] = "pending"
    step: int = Field(default=0, ge=0, le=6)
    skipped: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=list, max_length=7)


@router.get("/onboarding", response_model=OnboardingProgress)
async def get_onboarding(user: CurrentUser):
    return OnboardingProgress.model_validate(user.onboarding or {})


@router.put("/onboarding", response_model=OnboardingProgress)
async def save_onboarding(body: OnboardingProgress, user: CurrentUser, db: Database):
    account = await db.scalar(select(User).where(User.id == user.id).with_for_update())
    account.onboarding = body.model_dump()
    await db.commit()
    return body
