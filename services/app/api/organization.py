from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import select

from app.api.dependencies import Admin, Database
from app.db.models import AuditEvent, OrganizationSettings, User
from app.domain.operations import transaction_lock
from app.importing.examples import naming_examples
from app.importing.naming import (
    TOKENS,
    ImportGroup,
    ImportPlan,
    NamingProfile,
    StrictModel,
    fingerprint,
    plan_import,
)
from app.importing.settings import current_profile
from app.importing.storage import shared_naming_media

router = APIRouter(prefix="/organization", tags=["organization"])


class SettingsView(StrictModel):
    profile: NamingProfile
    revision: str
    tokens: dict[str, str]
    publication_available: bool = False


class SaveSettings(StrictModel):
    profile: NamingProfile
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class PreviewInput(StrictModel):
    profile: NamingProfile | None = None
    groups: list[ImportGroup] | None = Field(default=None, min_length=1, max_length=100)
    destinations: dict[Literal["ebook", "audio"], UUID] = Field(default_factory=dict)


@router.get("/settings", response_model=SettingsView)
async def settings(admin: Admin, db: Database):
    profile = await current_profile(db)
    return SettingsView(profile=profile, revision=fingerprint(profile.model_dump()), tokens=TOKENS)


@router.put("/settings", response_model=SettingsView)
async def save_settings(body: SaveSettings, admin: Admin, db: Database):
    await transaction_lock(db, "organization:settings")
    actor = await db.get(User, admin.id, populate_existing=True)
    if not actor.active or actor.role != "admin":
        raise HTTPException(403, "Administrator access is required")
    profile = await current_profile(db)
    if fingerprint(profile.model_dump()) != body.expected_revision:
        raise HTTPException(409, "Naming settings changed. Reload before saving your changes.")
    row = await db.scalar(select(OrganizationSettings).where(OrganizationSettings.id == 1))
    if not row:
        row = OrganizationSettings(id=1)
        db.add(row)
    row.profile = body.profile.model_dump()
    db.add(
        AuditEvent(
            actor_id=admin.id,
            action="organization.settings.changed",
            detail={"revision": fingerprint(row.profile)},
        )
    )
    await db.commit()
    return await settings(admin, db)


@router.get("/defaults", response_model=NamingProfile)
async def defaults(admin: Admin):
    return NamingProfile()


@router.post("/preview", response_model=ImportPlan)
async def preview(body: PreviewInput, admin: Admin, db: Database):
    try:
        groups = body.groups if body.groups is not None else naming_examples()
        return plan_import(
            groups,
            body.profile or await current_profile(db),
            shared_media=await shared_naming_media(
                db, {group.medium for group in groups}, body.destinations, require_choice=False
            ),
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
