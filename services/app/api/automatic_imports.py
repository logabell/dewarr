from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import select

from app.api.dependencies import Admin, Database
from app.config import get_settings
from app.db.models import AuditEvent, AutomaticImportPolicy, ImportDestination, User
from app.domain.operations import transaction_lock
from app.importing.destination_view import view as destination_view
from app.importing.naming import StrictModel
from app.importing.planning import assert_admin
from app.importing.settings import current_profile

router = APIRouter(prefix="/organization/destinations", tags=["organization"])


class PolicyInput(StrictModel):
    enabled: bool
    defer_until_verified: bool = False
    expected_generation: int = Field(ge=0)
    destination_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class PolicyView(StrictModel):
    enabled: bool
    requested_enabled: bool
    generation: int
    ready: bool
    can_enable: bool
    message: str


async def view(db, destination):
    policy = await db.scalar(
        select(AutomaticImportPolicy).where(AutomaticImportPolicy.destination_id == destination.id)
    )
    current = await destination_view(db, destination)
    conventional = (await current_profile(db)).layout == "conventional"
    can_enable = bool(
        current.publication_available and conventional and not get_settings().recovery_mode
    )
    approver = await db.get(User, policy.approved_by) if policy else None
    ready = bool(
        policy
        and policy.enabled
        and approver
        and approver.active
        and approver.role == "admin"
        and can_enable
        and policy.configuration["destination_revision"] == current.revision
        and policy.configuration["source_key"] == current.probe.get("source_key")
        and policy.configuration["source_path"] == current.probe.get("source_path")
    )
    requested = policy.configuration.get("requested_enabled", policy.enabled) if policy else True
    message = (
        "New completed downloads with clear catalog and file evidence can import automatically"
        if ready
        else "Automatic import is paused during recovery"
        if requested and get_settings().recovery_mode
        else "Automatic import requires the conventional layout in File naming"
        if requested and not conventional
        else "Folder verified. Enable automatic import to finish setup"
        if requested and current.publication_available
        else "Automatic import will turn on after folder verification"
        if requested
        else "Automatic importing is off; completed downloads use file review"
    )
    return PolicyView(
        enabled=bool(policy and policy.enabled),
        requested_enabled=requested,
        generation=policy.generation if policy else 0,
        ready=ready,
        can_enable=can_enable,
        message=message,
    )


async def save_setup_preference(db, destination, admin, enabled):
    """Remember setup intent without granting authority to import files."""
    await transaction_lock(db, f"automatic-policy:{destination.id}")
    policy = await db.scalar(
        select(AutomaticImportPolicy)
        .where(AutomaticImportPolicy.destination_id == destination.id)
        .with_for_update()
    )
    if not policy:
        policy = AutomaticImportPolicy(
            destination_id=destination.id, generation=0, approved_by=admin.id, configuration={}
        )
        db.add(policy)
    policy.generation += 1
    policy.enabled = False
    policy.approved_by = admin.id
    policy.configuration = {"requested_enabled": enabled}


@router.get("/{destination_id}/automatic-import", response_model=PolicyView)
async def detail(destination_id: UUID, admin: Admin, db: Database):
    destination = await db.get(ImportDestination, destination_id)
    if not destination:
        raise HTTPException(404, "Destination not found")
    return await view(db, destination)


@router.put("/{destination_id}/automatic-import", response_model=PolicyView)
async def save(destination_id: UUID, body: PolicyInput, admin: Admin, db: Database):
    await transaction_lock(db, f"automatic-policy:{destination_id}")
    await assert_admin(db, admin.id)
    destination = await db.get(ImportDestination, destination_id)
    if not destination:
        raise HTTPException(404, "Destination not found")
    policy = await db.scalar(
        select(AutomaticImportPolicy)
        .where(AutomaticImportPolicy.destination_id == destination.id)
        .with_for_update()
    )
    if (policy.generation if policy else 0) != body.expected_generation:
        raise HTTPException(409, "Automatic import settings changed; reload before saving")
    current = await destination_view(db, destination)
    if current.revision != body.destination_revision:
        raise HTTPException(409, "Destination changed; review its current settings")
    if (
        body.enabled
        and not body.defer_until_verified
        and not (await view(db, destination)).can_enable
    ):
        raise HTTPException(
            409, "Verify this route and use a certified layout before enabling automatic import"
        )
    if not policy:
        policy = AutomaticImportPolicy(
            destination_id=destination.id, approved_by=admin.id, generation=0, configuration={}
        )
        db.add(policy)
    policy.generation += 1
    policy.enabled, policy.approved_by = body.enabled and not body.defer_until_verified, admin.id
    if body.defer_until_verified:
        policy.configuration = {"requested_enabled": body.enabled}
    elif body.enabled:
        policy.configuration = {
            "destination_revision": current.revision,
            "source_key": current.probe["source_key"],
            "source_path": current.probe["source_path"],
        }
    else:
        policy.configuration = {}
    await db.flush()
    db.add(
        AuditEvent(
            actor_id=admin.id,
            action="organization.automatic.configured"
            if body.defer_until_verified
            else "organization.automatic.approved"
            if body.enabled
            else "organization.automatic.disabled",
            entity_id=policy.id,
            detail={"generation": policy.generation},
        )
    )
    await db.commit()
    return await view(db, destination)
