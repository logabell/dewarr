from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select

from app.db.models import Integration, Operation, RestoreCheckpoint
from app.domain.downloaders import TRANSFER_KINDS, mapped_path
from app.domain.recovery_approvals import denial
from app.importing.destinations import current_receipts, destination_configuration
from app.importing.naming import StrictModel, fingerprint
from app.importing.storage import import_sources, shared_library_roots


class ClientRouteView(StrictModel):
    downloader_id: UUID
    name: str
    download_path: str
    status: Literal["verified", "checking", "needs-verification", "unavailable", "failed"]
    message: str
    checked_at: str | None = None


class DestinationView(StrictModel):
    id: UUID
    root_key: str
    library_id: UUID
    medium: str
    backend_path: str
    local_path: str | None = None
    staging_path: str | None = None
    shared_root: bool = False
    mode: str
    seeding_rename: bool = False
    client_path: str | None = None
    enabled: bool
    revision: str
    configured: bool
    probe: dict[str, Any] | None
    publication_available: bool = False
    server_kind: str
    client_routes: list[ClientRouteView] = Field(default_factory=list)


async def view(db, row, *, include_routes=False):
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
    client_routes = await route_views(db, row, revision, probe) if include_routes else []
    return DestinationView(
        client_routes=client_routes,
        id=row.id,
        root_key=row.root_key,
        library_id=row.library_id,
        medium=row.medium,
        backend_path=row.backend_path,
        local_path=configuration["root_path"],
        staging_path=configuration["staging_path"],
        shared_root=bool(
            configuration["root_path"]
            and Path(configuration["root_path"]) in await shared_library_roots(db)
        ),
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


async def route_views(db, destination, revision, probe):
    from app.importing.route_evidence import receipts

    sources = await import_sources(db)
    result = []
    clients = await db.scalars(
        select(Integration)
        .where(
            Integration.kind.in_(TRANSFER_KINDS),
            Integration.owner_id.is_(None),
            Integration.deleted_at.is_(None),
            Integration.enabled.is_(True),
        )
        .order_by(Integration.name)
    )
    for client in clients:
        status, message, checked_at = "needs-verification", "Folder verification required", None
        mapping = None
        try:
            mapping = mapped_path(client, (client.config or {}).get("save_path", ""), sources)
        except HTTPException as error:
            status, message = "unavailable", str(error.detail)
        if client.status != "connected":
            status, message = "unavailable", "Connect and test this download client first"
        elif not destination.enabled:
            status, message = "unavailable", "Library folder disabled"
        elif destination.seeding_rename and client.kind != "qbittorrent":
            status, message = "unavailable", "Seeding rename requires qBittorrent"
        elif mapping:
            for receipt in receipts(probe):
                binding = receipt.get("setup_downloader", {})
                if (
                    receipt.get("status") == "verified"
                    and binding.get("id") == str(client.id)
                    and binding.get("mapping") == mapping
                ):
                    status, message = "verified", "Download folder → library verified"
                    checked_at = receipt.get("checked_at")
            if status != "verified":
                operation = await db.scalar(
                    select(Operation)
                    .where(
                        Operation.kind == "organization.probe",
                        Operation.payload["destination_id"].astext == str(destination.id),
                        Operation.payload["setup_downloader"]["id"].astext == str(client.id),
                    )
                    .order_by(Operation.created_at.desc())
                    .limit(1)
                )
                if (
                    operation
                    and operation.payload.get("setup_downloader", {}).get("generation")
                    == client.credential_generation
                    and operation.payload.get("setup_downloader", {}).get("mapping") == mapping
                    and fingerprint(operation.payload.get("configuration", {})) == revision
                ):
                    if operation.status in {"queued", "running"}:
                        status, message = "checking", "Checking download folder → library…"
                    elif operation.status == "failed":
                        status, message = (
                            "failed",
                            operation.message or "Folder verification failed",
                        )
        result.append(
            ClientRouteView(
                downloader_id=client.id,
                name=client.name,
                download_path=(client.config or {}).get("save_path", ""),
                status=status,
                message=message,
                checked_at=checked_at,
            )
        )
    return result
