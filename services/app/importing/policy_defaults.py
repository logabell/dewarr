"""Enable imports when an administrator verifies a configured download folder."""

from sqlalchemy import select

from app.config import get_settings
from app.db.models import AuditEvent, AutomaticImportPolicy, User
from app.importing.destination_view import view
from app.importing.route_evidence import approval
from app.importing.settings import current_profile


async def enable_verified_imports(db, destination, actor_id):
    """Called under the policy and destination locks after a successful setup probe."""
    policy = await db.scalar(
        select(AutomaticImportPolicy)
        .where(AutomaticImportPolicy.destination_id == destination.id)
        .with_for_update()
    )
    if policy and not policy.configuration.get("requested_enabled", policy.enabled):
        return  # Preserve an explicit opt-out, including one saved during the probe.
    current = await view(db, destination)
    actor = await db.get(User, actor_id, populate_existing=True)
    if (
        get_settings().recovery_mode
        or not actor
        or not actor.active
        or actor.role != "admin"
        or not current.publication_available
        or (await current_profile(db)).layout != "conventional"
    ):
        return
    approver = await db.get(User, policy.approved_by) if policy else None
    if (
        policy
        and policy.enabled
        and policy.configuration.get("destination_revision") == current.revision
        and approver
        and approver.active
        and approver.role == "admin"
    ):
        # Another verified client belongs to the same enabled destination. Do not
        # invalidate downloads already using its policy generation.
        return
    if not policy:
        policy = AutomaticImportPolicy(destination_id=destination.id, generation=0)
        db.add(policy)
    policy.enabled = True
    policy.approved_by = actor_id
    policy.generation += 1
    policy.configuration = {"destination_revision": current.revision, **approval(current.probe)}
    await db.flush()
    db.add(
        AuditEvent(
            actor_id=actor_id,
            action="organization.automatic.approved",
            entity_id=policy.id,
            detail={"generation": policy.generation, "via": "verified-download-folder"},
        )
    )
