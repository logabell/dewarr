"""Reconcile a published child's local ledger without publishing or scheduling work."""

import asyncio
import math
from datetime import UTC, datetime
from functools import partial
from pathlib import PurePosixPath
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.adapters.audiobookshelf import ABSItem, Audiobookshelf
from app.adapters.grimmory import Grimmory
from app.db.models import (
    AuditEvent,
    ImportDestination,
    ImportEntry,
    Integration,
    Library,
    LibraryAsset,
    Operation,
    ProviderObject,
    RecoveryFinding,
    Version,
)
from app.db.session import session_factory
from app.domain import capacity
from app.domain import recovery_observers as observers
from app.domain import recovery_reconciliation as reviews
from app.domain.recovery_scans import ScanHeld, digest
from app.importing.collection_contents import verify as verify_contents
from app.importing.destinations import destination_configuration
from app.importing.execution import (
    confirm_observation,
    grimmory_relocated_match,
    matches,
    observe_cover,
    published_file,
)
from app.importing.publication import PublicationError, PublicationSpec
from app.importing.recovery import read_publication
from app.importing.storage import storage_settings
from app.importing.versioning import version_revision
from app.security import decrypt_secrets

KIND = reviews.PUBLICATION_KIND
WITHDRAWN = {"cancelled", "cancelling", "cancel-held", "skipped"}


def reference(finding):
    return {"finding_id": str(finding.id), "finding_digest": reviews.finding_signature(finding)}


async def confirmation_context(db, entry):
    """Validate identity/route only; this never authorizes replay of the old request."""
    destination = await db.get(ImportDestination, entry.destination_id)
    library = await db.get(Library, destination.library_id) if destination else None
    integration = await db.get(Integration, library.integration_id) if library else None
    if (
        not destination
        or not destination.enabled
        or not library
        or not library.accessible
        or not integration
        or not integration.enabled
        or integration.kind not in {"audiobookshelf", "grimmory"}
    ):
        raise ScanHeld("Restore the current destination and library access before confirmation")
    if await destination_configuration(db, destination) != entry.configuration["destination"]:
        raise ScanHeld("The frozen destination changed; publication can be recorded separately")
    version = await db.get(Version, entry.version_id)
    if not version or version_revision(version) != entry.expected_metadata["version_revision"]:
        raise ScanHeld("The catalog version changed; reconcile its identity before confirmation")
    conflict = await db.scalar(
        select(ProviderObject.id)
        .where(
            ProviderObject.version_id == version.id,
            ProviderObject.match_status == "needs-review",
        )
        .limit(1)
    )
    if conflict:
        raise ScanHeld("Resolve this version's metadata conflict before confirmation")
    await verify_contents(db, entry.expected_metadata.get("collection_contents", []))
    return integration, library


def _file_identity_matches(file, identity, *, grimmory: bool) -> bool:
    if file is None:
        return False
    kilobytes = getattr(file, "size_unit", "byte") == "kilobyte"
    if kilobytes:
        return abs(file.size - identity["size"]) < 1024
    # Grimmory track listings are exact byte sizes without inode or modification time.
    if grimmory and file.inode is None and file.modified is None:
        return file.size == identity["size"]
    return (
        file.inode == str(identity["inode"])
        and file.size == identity["size"]
        and file.modified is not None
        and math.isfinite(file.modified)
        # ABS stores JavaScript millisecond timestamps; allow only rounding.
        and abs(file.modified - identity["mtime_ns"] / 1_000_000) <= 1
    )


def match_files(entry, item, evidence):
    grimmory = str(getattr(item, "cover_path", "") or "").startswith("grimmory:")
    if evidence.get("relocated"):
        if not grimmory or not grimmory_relocated_match(entry, item):
            raise ScanHeld("The published files are no longer in their folder")
        return
    if not matches(entry, item):
        raise ScanHeld("The backend item is outside this published book's folder")
    local = evidence.get("media_identities", {})
    if set(local) != {file["name"] for file in entry.specification["files"]}:
        raise ScanHeld("Observe fresh publication file identities before confirmation")
    used = []
    for name, identity in local.items():
        file = published_file(item, name, identity, used, grimmory=grimmory)
        if file is not None:
            used.append(file)
        if not _file_identity_matches(file, identity, grimmory=grimmory):
            raise ScanHeld("Library file identities do not corroborate the verified publication")


def publication_candidates(
    entry, findings, library_external_id, folder, *, grimmory: bool, relocated: bool = False
):
    """Prefer the published folder. A Grimmory pattern rename is the only other match."""

    def relevant(finding) -> bool:
        evidence = finding.evidence
        return (
            evidence.get("external_library_id") == library_external_id
            and evidence.get("medium") == entry.expected_metadata["medium"]
            and isinstance(evidence.get("item"), dict)
        )

    rows = [finding for finding in findings if relevant(finding)]
    placed = [finding for finding in rows if finding.evidence["item"].get("path") == folder]
    if placed or not grimmory:
        return placed
    moved = []
    for finding in rows:
        try:
            observed = ABSItem.model_validate(finding.evidence["item"])
        except ValidationError:
            continue
        try:
            matched = (
                grimmory_relocated_match(entry, observed) if relocated else matches(entry, observed)
            )
        except PublicationError:
            continue
        if matched:
            moved.append(finding)
    return moved


async def confirmation_preview(db, scan_id, entry, evidence):
    integration, library = await confirmation_context(db, entry)
    findings = list(
        await db.scalars(
            select(RecoveryFinding).where(
                RecoveryFinding.scan_id == scan_id,
                RecoveryFinding.domain == "library",
                RecoveryFinding.evidence["integration_id"].astext == str(integration.id),
            )
        )
    )
    ready = [finding for finding in findings if finding.state == "inventory-ready"]
    scope = [
        finding
        for finding in findings
        if finding.evidence.get("external_library_id") == library.external_id
        and "scope_fingerprint" in finding.evidence
    ]
    if len(ready) != 1 or len(scope) != 1:
        raise ScanHeld("A complete current ABS observation is needed before confirmation")
    if scope[0].evidence["scope_fingerprint"] != library.scope_fingerprint:
        raise ScanHeld("Review the current library inventory and permissions before confirmation")
    folder = str(
        PurePosixPath(entry.configuration["destination"]["backend_path"])
        / entry.specification["folder"]
    )

    candidates = publication_candidates(
        entry,
        findings,
        library.external_id,
        folder,
        grimmory=integration.kind == "grimmory",
        relocated=bool(evidence.get("relocated")),
    )
    if len(candidates) != 1:
        raise ScanHeld("ABS has not uniquely detected this complete published book")
    candidate = candidates[0]
    item = ABSItem.model_validate(candidate.evidence["item"])
    match_files(entry, item, evidence)
    suppressed = await db.scalar(
        select(LibraryAsset.id).where(
            LibraryAsset.library_id == library.id,
            LibraryAsset.external_id == item.id,
            LibraryAsset.medium == entry.expected_metadata["medium"],
            LibraryAsset.state == "intentionally-removed",
        )
    )
    if suppressed:
        raise ScanHeld("This library item was intentionally suppressed; keep it awaiting review")
    link = await db.scalar(
        select(ProviderObject).where(
            ProviderObject.provider
            == f"{'grimmory' if integration.kind == 'grimmory' else 'abs'}:{integration.id}",
            ProviderObject.kind == "item:" + entry.expected_metadata["medium"],
            ProviderObject.external_id == item.id,
        )
    )
    if link and link.manual_lock and link.version_id != entry.version_id:
        raise ScanHeld("A manual library match conflicts with this imported version")
    return {
        "integration_id": str(integration.id),
        "library_id": str(library.id),
        "external_library_id": library.external_id,
        "external_item_id": item.id,
        "connection_signature": reviews.connection_signature(integration),
        "inventory_digest": ready[0].evidence["inventory_digest"],
        "scope": scope[0].evidence["scope_fingerprint"],
        "item_digest": digest(item.model_dump(mode="json")),
        "additional_findings": [reference(row) for row in [ready[0], scope[0], candidate]],
    }


async def prepare(db, checkpoint, owner_id, scan_id, finding_ids, key):
    old, scan, command = await reviews.review_inputs(
        db, checkpoint, owner_id, scan_id, finding_ids, key, kind=KIND
    )
    if old:
        return old
    items, seen = [], set()
    for finding_id in sorted(finding_ids):
        finding = await db.get(RecoveryFinding, finding_id)
        if (
            not finding
            or finding.scan_id != scan.id
            or finding.domain != "files"
            or finding.state not in {"published", "relocated"}
            or not finding.entity_id
        ):
            raise HTTPException(409, "Choose a verified published book observation")
        entry = await db.get(ImportEntry, finding.entity_id)
        if not entry or not entry.specification or not entry.configuration or entry.id in seen:
            raise HTTPException(409, "Each frozen publication must appear exactly once")
        if finding.state == "relocated":
            destination = await db.get(ImportDestination, entry.destination_id)
            library = await db.get(Library, destination.library_id) if destination else None
            integration = await db.get(Integration, library.integration_id) if library else None
            if not integration or integration.kind != "grimmory":
                raise HTTPException(409, "Choose a verified published book observation")
        seen.add(entry.id)
        proof = {}
        outcome, reason = "awaiting-library", "Record publication; ABS confirmation is pending"
        if entry.state in WITHDRAWN:
            outcome, reason = "cancel-held", "Published files exist for a withdrawn import"
        else:
            try:
                proof = await confirmation_preview(db, scan.id, entry, finding.evidence)
                outcome, reason = "confirmed", "Verified files and ABS evidence match this version"
            except (ScanHeld, PublicationError, HTTPException) as error:
                reason = str(error.detail) if isinstance(error, HTTPException) else str(error)
        items.append(
            {
                **reference(finding),
                **proof,
                "entry_id": str(entry.id),
                "title": entry.expected_metadata["title"],
                "medium": entry.expected_metadata["medium"],
                "folder": entry.specification["folder"],
                "saved_state": entry.state,
                "outcome": outcome,
                "reason": reason,
                "file_evidence_digest": digest(finding.evidence),
            }
        )
    return await reviews.save_review(
        db,
        checkpoint,
        owner_id,
        scan,
        command,
        items,
        key,
        kind=KIND,
        message="Review each published book; files and automation remain unchanged",
    )


async def fresh_publications(identifier, token, payload):
    observed = {}
    for item in payload["items"]:
        await reviews.pulse(identifier, token)
        async with session_factory()() as db:
            entry = await db.get(ImportEntry, UUID(item["entry_id"]))
            roots = (await storage_settings(db)).model_dump(
                mode="json",
                include={
                    "import_sources",
                    "import_destinations",
                    "import_staging_root",
                    "import_storage_routes",
                    "import_journal_root",
                },
            )
            saved = {"id": entry.id, "state": entry.state, "specification": entry.specification}
            if item["outcome"] == "confirmed":
                integration, library = await confirmation_context(db, entry)
                if reviews.connection_signature(integration) != item["connection_signature"]:
                    raise ScanHeld("The reviewed backend connection changed")
                endpoint = integration.base_url
                secrets = decrypt_secrets(integration.encrypted_secrets)
                client_type = Grimmory if integration.kind == "grimmory" else Audiobookshelf
                secret = secrets if integration.kind == "grimmory" else secrets["token"]
        state, _, evidence, receipt = await asyncio.to_thread(read_publication, saved, roots)
        if (
            state not in {"published", "relocated"}
            or digest(evidence) != item["file_evidence_digest"]
        ):
            raise ScanHeld("Publication files or journal changed; observe and review again")
        record = None
        if item["outcome"] == "confirmed":
            async with asyncio.timeout(600), client_type(endpoint, secret) as client:
                current = await observers.read_inventory(
                    client, partial(reviews.pulse, identifier, token)
                )
                configuration = await client.import_configuration(item["external_library_id"])
                _, final_scope = await client.authorize()
            if (
                observers.inventory_signature(current) != item["inventory_digest"]
                or current["scope"] != item["scope"]
                or final_scope != item["scope"]
                or entry.configuration["destination"]["backend_path"] not in configuration.folders
                or (item["medium"] == "ebook" and configuration.audiobooks_only)
            ):
                raise ScanHeld("ABS inventory, permissions or library route changed since review")
            candidates = [
                row
                for row in current["items"].get(item["external_library_id"], [])
                if row.id == item["external_item_id"]
            ]
            if (
                len(candidates) != 1
                or digest(candidates[0].model_dump(mode="json")) != item["item_digest"]
            ):
                raise ScanHeld("The reviewed ABS item changed")
            record = candidates[0]
            match_files(entry, record, evidence)
            # Remote reads cannot authorize a publication that changed while they ran.
            again = await asyncio.to_thread(read_publication, saved, roots)
            if (
                again[0] not in {"published", "relocated"}
                or digest(again[2]) != item["file_evidence_digest"]
            ):
                raise ScanHeld("Publication changed during backend confirmation")
        cover = await asyncio.to_thread(
            observe_cover, PublicationSpec.model_validate(entry.specification)
        )
        observed[item["finding_id"]] = {"receipt": receipt, "record": record, "cover": cover}
    return observed


async def record_publication(db, operation, item, observed):
    entry = await db.get(ImportEntry, UUID(item["entry_id"]), with_for_update=True)
    before = {"state": entry.state, "published_at": str(entry.published_at or "")}
    entry.receipt = observed["receipt"]
    # This timestamp is when recovery observed publication, not an invented historical time.
    entry.published_at = entry.published_at or datetime.now(UTC)
    entry.run_token, entry.next_check_at = None, None
    entry.state, entry.message = item["outcome"], item["reason"]
    if item["outcome"] == "confirmed":
        integration, library = await confirmation_context(db, entry)
        await confirm_observation(
            db,
            entry,
            integration,
            library,
            observed["record"],
            observed["cover"],
            operation.owner_id,
        )
    else:
        entry.confirmed_at, entry.asset_id = None, None
    await capacity.release_import(db, entry)
    if entry.operation_id:
        original = await db.get(Operation, entry.operation_id, with_for_update=True)
        original.status = "completed" if entry.state == "confirmed" else "attention"
        original.message = entry.message + "; recovery remains paused"
    db.add(
        AuditEvent(
            actor_id=operation.owner_id,
            action="recovery.publication.reconciled",
            entity_id=entry.id,
            detail={
                "review_id": str(operation.id),
                "finding_id": item["finding_id"],
                "before": before,
                "state": entry.state,
                "publication_observed_at": datetime.now(UTC).isoformat(),
                "asset_id": str(entry.asset_id) if entry.asset_id else None,
            },
        )
    )
    return {
        "entry_id": str(entry.id),
        "state": entry.state,
        "asset_id": str(entry.asset_id) if entry.asset_id else None,
    }


async def run(identifier):
    await reviews.run_review(
        identifier,
        kind=KIND,
        read=fresh_publications,
        apply=record_publication,
        message="Selected publications recorded. Run fresh observations before further review; "
        "automation remains paused.",
    )
