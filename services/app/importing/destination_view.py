from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.db.models import RestoreCheckpoint
from app.domain.recovery_approvals import denial
from app.importing.destinations import current_receipts, destination_configuration
from app.importing.naming import StrictModel, fingerprint


class DestinationView(StrictModel):
    id: UUID
    root_key: str
    library_id: UUID
    medium: str
    backend_path: str
    local_path: str | None = None
    mode: str
    seeding_rename: bool = False
    client_path: str | None = None
    enabled: bool
    revision: str
    configured: bool
    probe: dict[str, Any] | None
    publication_available: bool = False
    server_kind: str


async def view(db, row):
    configuration = await destination_configuration(db, row)
    revision = fingerprint(configuration)
    valid = await current_receipts(db, row.probe, revision)
    probe = (
        (
            {**valid[-1], "download_routes": valid}
            if row.probe and "download_routes" in row.probe
            else valid[0]
        )
        if valid
        else None
    )
    if (
        not probe
        and row.probe
        and row.probe.get("status") == "failed"
        and row.probe.get("configuration_revision") == revision
    ):
        probe = row.probe
    if probe:
        historical = (
            await denial(db, "operation", row.probe_operation_id)
            if row.probe_operation_id
            else await db.scalar(select(RestoreCheckpoint.id).limit(1))
        )
        if historical:
            # Preserve the receipt on disk/in the ledger, but require a new route test.
            probe = None
    return DestinationView(
        id=row.id,
        root_key=row.root_key,
        library_id=row.library_id,
        medium=row.medium,
        backend_path=row.backend_path,
        local_path=configuration["root_path"],
        mode=row.mode,
        seeding_rename=bool(row.seeding_rename),
        client_path=row.client_path,
        enabled=row.enabled,
        revision=revision,
        configured=bool(configuration["root_path"] and configuration["staging_path"]),
        probe=probe,
        publication_available=bool(
            probe
            and probe.get("status") == "verified"
            and probe.get("backend", {}).get("root_mapping")
            and row.enabled
        ),
        server_kind=(configuration["backend"] or {}).get("kind") or "audiobookshelf",
    )
