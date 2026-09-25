"""Durable transfer slots, rolling automatic budgets and shared filesystem reservations."""

import asyncio
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, text

from app.db.models import (
    AutomaticImport,
    CapacitySettings,
    DownloadAttempt,
    DownloadCapacity,
    DownloadHandoff,
    FrozenImportPlan,
    ImportCapacity,
    ImportEntry,
    ImportRun,
)
from app.db.session import session_factory
from app.domain.operations import transaction_lock
from app.importing.filesystem import InspectionError, directory, relative_parts
from app.importing.storage import import_sources

LOCK = "acquisition:capacity"
MIB = 1024**2


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_transfers: int = Field(default=3, ge=1, le=100)
    automatic_per_day: int = Field(default=10, ge=1, le=10000)
    minimum_free_bytes: int = Field(default=5 * 1024**3, ge=0, le=2**53 - 1)
    minimum_free_percent: int = Field(default=5, ge=0, le=50)


class CapacityWait(Exception):
    """Safe user-facing reason; the existing attempt remains queued."""


async def settings(db):
    row = await db.get(CapacitySettings, 1, populate_existing=True)
    return Limits.model_validate(row.configuration) if row else Limits()


async def storage_generation(db):
    return (
        await db.scalar(select(CapacitySettings.storage_generation).where(CapacitySettings.id == 1))
        or 0
    )


async def consumed(db):
    """Invalidate pre-consumption disk snapshots in the same locked transaction."""
    row = await db.get(CapacitySettings, 1, populate_existing=True)
    if not row:
        row = CapacitySettings(id=1, configuration=Limits().model_dump(), storage_generation=0)
        db.add(row)
    row.storage_generation += 1
    await db.flush()


async def current_observation(db, observation):
    if observation.get("generation", 0) != await storage_generation(db):
        raise CapacityWait("Storage accounting changed; waiting for a fresh capacity observation")


def read_mount(path, relative=""):
    """Follow only real directories; a not-yet-created save path uses its ancestor."""
    with directory(Path(path)) as root:
        current = os.dup(root)
        try:
            for part in relative_parts(relative) if relative not in {"", "."} else []:
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                    )
                except FileNotFoundError:
                    break
                os.close(current)
                current = child
            info, space = os.fstat(current), os.fstatvfs(current)
            key = f"{info.st_dev}:{getattr(space, 'f_fsid', info.st_dev)}"
            return key, {
                "available": space.f_bavail * space.f_frsize,
                "total": space.f_blocks * space.f_frsize,
            }
        finally:
            os.close(current)


def measure(paths):
    roots, filesystems = {}, {}
    for name, (path, relative) in paths.items():
        key, values = read_mount(path, relative)
        roots[name] = key
        previous = filesystems.get(key, values)
        filesystems[key] = {k: min(previous[k], values[k]) for k in values}
    return {"roots": roots, "filesystems": filesystems, "at": datetime.now(UTC).isoformat()}


async def observe(paths):
    async with session_factory()() as db:
        generation = await storage_generation(db)
    try:
        async with asyncio.timeout(10):
            observation = await asyncio.to_thread(measure, paths)
            observation["generation"] = generation
            return observation
    except (OSError, InspectionError, TimeoutError) as error:
        raise CapacityWait("Storage capacity is unavailable; check worker mounts") from error


async def observe_download(frozen):
    mapping, destination = frozen["mapping"], frozen["destination"]
    async with session_factory()() as db:
        configured = (await import_sources(db)).get(mapping["source_key"])
    if not configured or str(Path(configured) / mapping["relative_path"]) != mapping["worker_path"]:
        raise CapacityWait("Download storage mapping changed; review the saved route")
    return await observe(
        {
            "download": (configured, mapping["relative_path"]),
            "library": (destination["root_path"], ""),
            "staging": (destination["staging_path"], ""),
        }
    )


def download_cost(frozen, observation):
    roots = observation["roots"]
    if roots["staging"] != roots["library"]:
        raise CapacityWait("Staging and library must be on the same filesystem")
    rename = bool(frozen["destination"].get("seeding_rename"))
    same_library = roots["download"] == roots["library"]
    if not rename and frozen["destination"]["mode"] == "hardlink" and not same_library:
        raise CapacityWait("Hardlinks require download and library storage on the same filesystem")
    descriptor = frozen["descriptor"]
    total = descriptor.get("torrent_bytes")
    if total is None:
        total = descriptor.get("content_bytes") or frozen.get("release", {}).get("size_bytes") or 0
    if not total:
        raise CapacityWait("Usenet release size is unknown; review it before downloading")
    # Future sidecar/cover allowance is conservative until actual import plans exist.
    overhead = min(len(frozen["descriptor"]["files"]), 100) * 8 * MIB
    copies = frozen["destination"]["mode"] == "copy" or (rename and not same_library)
    future = overhead + (total if copies else 0)
    return {roots["download"]: total}, {roots["library"]: future}


async def reserved_bytes(db, *, excluding_attempt=None, excluding_entry=None):
    # Aggregate inside PostgreSQL: memory and transfer scale with mount count,
    # not with every active transfer and import reservation.
    rows = await db.execute(
        text("""
            SELECT resource.key, sum(resource.value::numeric) AS amount
            FROM (
                SELECT resources FROM download_capacity
                WHERE (CAST(:attempt AS uuid) IS NULL OR attempt_id != CAST(:attempt AS uuid))
                UNION ALL
                SELECT import_resources FROM download_capacity
                WHERE (CAST(:attempt AS uuid) IS NULL OR attempt_id != CAST(:attempt AS uuid))
                UNION ALL
                SELECT resources FROM import_capacity
                WHERE (CAST(:entry AS uuid) IS NULL OR entry_id != CAST(:entry AS uuid))
            ) AS claims
            CROSS JOIN LATERAL jsonb_each_text(claims.resources) AS resource
            GROUP BY resource.key
        """),
        {
            "attempt": str(excluding_attempt) if excluding_attempt else None,
            "entry": str(excluding_entry) if excluding_entry else None,
        },
    )
    return defaultdict(int, {key: int(amount) for key, amount in rows})


def verify_space(observation, required, reserved, limits):
    if datetime.now(UTC) - datetime.fromisoformat(observation["at"]) > timedelta(seconds=10):
        raise CapacityWait("Storage observation expired; capacity will be checked again")
    for key, amount in required.items():
        space = observation["filesystems"].get(key)
        if not space or space["total"] <= 0:
            raise CapacityWait("Storage capacity is unknown; waiting for a fresh observation")
        floor = max(limits.minimum_free_bytes, space["total"] * limits.minimum_free_percent // 100)
        if space["available"] - reserved.get(key, 0) - amount < floor:
            raise CapacityWait(
                "Waiting for free disk space after existing reservations and reserve"
            )


def combine(*values):
    result = defaultdict(int)
    for value in values:
        for key, amount in value.items():
            result[key] += amount
    return dict(result)


async def require_observed_imports(db, *, excluding_entry=None):
    unknown = (
        select(ImportCapacity.entry_id)
        .join(ImportEntry)
        .where(
            ImportCapacity.observed_mounts == {},
            ImportEntry.reserved.is_(True),
            ImportEntry.published_at.is_(None),
            ImportEntry.state.not_in(["cancelled", "skipped"]),
        )
    )
    if excluding_entry:
        unknown = unknown.where(ImportEntry.id != excluding_entry)
    if await db.scalar(unknown.limit(1)):
        raise CapacityWait("Existing import storage needs reconciliation before new work")


async def admit(db, attempt, selection, observation):
    await transaction_lock(db, LOCK)
    limits = await settings(db)
    row = await db.get(DownloadCapacity, attempt.id, populate_existing=True)
    if not row:
        row = DownloadCapacity(attempt_id=attempt.id)
        db.add(row)
        await db.flush()
    downloads, imports = download_cost(selection.frozen, observation)
    if row.observed_mounts and row.observed_mounts != observation["roots"]:
        raise CapacityWait("Storage mount identity changed; review the saved route")
    # Historical submissions still need accounting, even when already over capacity.
    if not attempt.external_may_exist:
        await current_observation(db, observation)
        older = await db.scalar(
            select(DownloadAttempt.id)
            .join(DownloadCapacity)
            .where(
                DownloadAttempt.endpoint_key == attempt.endpoint_key,
                DownloadAttempt.id != attempt.id,
                DownloadAttempt.state.in_(["queued", "preflight"]),
                DownloadAttempt.external_may_exist.is_(False),
                DownloadCapacity.slot_active.is_(False),
                DownloadAttempt.next_check_at <= datetime.now(UTC),
                or_(
                    DownloadAttempt.created_at < attempt.created_at,
                    (DownloadAttempt.created_at == attempt.created_at)
                    & (DownloadAttempt.id < attempt.id),
                ),
            )
            .limit(1)
        )
        if older:
            raise CapacityWait("Waiting for an earlier queued download to check capacity")
        occupied = await db.scalar(
            select(func.count())
            .select_from(DownloadAttempt)
            .outerjoin(DownloadCapacity)
            .where(
                DownloadAttempt.endpoint_key == attempt.endpoint_key,
                DownloadAttempt.id != attempt.id,
                or_(
                    DownloadCapacity.slot_active.is_(True),
                    DownloadCapacity.attempt_id.is_(None)
                    & DownloadAttempt.external_may_exist.is_(True)
                    & (DownloadAttempt.state != "complete"),
                ),
            )
        )
        if occupied >= limits.active_transfers:
            raise CapacityWait("Waiting for a free downloader slot")
        if row.automatic:
            debit = await db.scalar(
                select(func.count())
                .select_from(DownloadCapacity)
                .join(DownloadAttempt)
                .where(
                    DownloadCapacity.attempt_id != attempt.id,
                    DownloadCapacity.automatic.is_(True),
                    or_(
                        DownloadCapacity.submitted_at >= datetime.now(UTC) - timedelta(hours=24),
                        DownloadCapacity.slot_active.is_(True),
                    ),
                )
            )
            if debit >= limits.automatic_per_day:
                raise CapacityWait("Waiting for the rolling 24-hour automatic transfer budget")
        # Do not spend storage whose prior external reservations are still unknown.
        if await db.scalar(
            select(DownloadAttempt.id)
            .outerjoin(DownloadCapacity)
            .where(
                DownloadAttempt.id != attempt.id,
                DownloadAttempt.external_may_exist.is_(True),
                DownloadAttempt.state != "complete",
                or_(
                    DownloadCapacity.observed_mounts == {},
                    DownloadCapacity.attempt_id.is_(None),
                ),
            )
            .limit(1)
        ):
            raise CapacityWait(
                "Existing transfer storage needs reconciliation before new downloads"
            )
        await require_observed_imports(db)
        verify_space(
            observation,
            combine(downloads, imports),
            await reserved_bytes(db, excluding_attempt=attempt.id),
            limits,
        )
    row.resources, row.import_resources = downloads, imports
    row.observed_mounts, row.slot_active = observation["roots"], True


async def submitted(db, attempt):
    await transaction_lock(db, LOCK)
    row = await db.get(DownloadCapacity, attempt.id)
    if not row or not row.slot_active:
        raise CapacityWait("A durable capacity reservation is required before submission")
    row.submitted_at = row.submitted_at or datetime.now(UTC)


async def release_slot(db, attempt):
    """Free a slot after a submitted transfer has stopped without a library import."""
    await transaction_lock(db, LOCK)
    row = await db.get(DownloadCapacity, attempt.id)
    if row:
        row.slot_active, row.resources, row.import_resources = False, {}, {}


async def release_unsubmitted(db, attempt):
    if attempt.external_may_exist:
        return
    await transaction_lock(db, LOCK)
    row = await db.get(DownloadCapacity, attempt.id)
    if row:
        row.slot_active, row.resources, row.import_resources = False, {}, {}


async def downloaded(db, attempt, *, needs_import=True):
    await transaction_lock(db, LOCK)
    row = await db.get(DownloadCapacity, attempt.id)
    if row:
        if row.resources:
            await consumed(db)
        row.slot_active, row.resources = False, {}
        if not needs_import:
            row.import_resources = {}


async def observe_import(spec):
    from app.importing.publication import remaining_import_bytes

    remaining = await asyncio.to_thread(remaining_import_bytes, spec)
    observation = await observe(
        {"library": (spec.destination_root, ""), "staging": (spec.staging_root, "")}
    )
    observation["required_bytes"] = remaining + MIB
    return observation


async def observe_publication(spec):
    return await observe(
        {"library": (spec.destination_root, ""), "staging": (spec.staging_root, "")}
    )


async def reconcile_import(db, entry, observation):
    """Commit observed consumption even when another reservation blocks admission."""
    await transaction_lock(db, LOCK)
    row = await db.get(ImportCapacity, entry.id, populate_existing=True)
    if row and row.observed_mounts == observation["roots"] and row.resources:
        key = observation["roots"]["library"]
        updated = {key: min(row.resources.get(key, 0), observation["required_bytes"])}
        if row.resources != updated:
            await consumed(db)
            row.resources = updated


async def staged_import(db, entry, observation):
    """Called only after the publisher verifies the complete private staging item.

    Those bytes are now reflected in statvfs. Commit their consumption before
    checking other reservations, otherwise concurrent complete copies can each
    count the other's already-written bytes a second time and never publish.
    """
    await transaction_lock(db, LOCK)
    row = await db.get(ImportCapacity, entry.id, populate_existing=True)
    if not row or not row.resources or row.observed_mounts != observation["roots"]:
        raise CapacityWait("Import storage reservation changed; review the route")
    updated = {observation["roots"]["library"]: MIB}
    if row.resources != updated:
        await consumed(db)
        row.resources = updated


async def publication_capacity(db, entry, observation):
    await transaction_lock(db, LOCK)
    await current_observation(db, observation)
    row = await db.get(ImportCapacity, entry.id, populate_existing=True)
    if not row or row.observed_mounts != observation["roots"] or not row.resources:
        raise CapacityWait("Import storage reservation changed; review the route")
    verify_space(
        observation,
        row.resources,
        await reserved_bytes(db, excluding_entry=entry.id),
        await settings(db),
    )


async def import_parent(db, entry):
    run = await db.get(ImportRun, entry.run_id)
    plan = await db.get(FrozenImportPlan, run.plan_id)
    return await db.scalar(
        select(DownloadAttempt).where(
            or_(
                DownloadAttempt.inspection_id == plan.inspection_id,
                DownloadAttempt.id.in_(
                    select(DownloadHandoff.attempt_id).where(
                        DownloadHandoff.inspection_id == plan.inspection_id
                    )
                ),
                DownloadAttempt.id.in_(
                    select(AutomaticImport.attempt_id).where(
                        AutomaticImport.inspection_id == plan.inspection_id
                    )
                ),
            )
        )
    )


async def reserve_import(db, entry, spec, observation):
    parent = await import_parent(db, entry)
    await transaction_lock(db, LOCK)
    await current_observation(db, observation)
    if observation["roots"]["library"] != observation["roots"]["staging"]:
        raise CapacityWait("Staging and library must share a filesystem")
    key = observation["roots"]["library"]
    amount = observation["required_bytes"]
    row = await db.get(ImportCapacity, entry.id, populate_existing=True)
    if row and row.observed_mounts and row.observed_mounts != observation["roots"]:
        raise CapacityWait("Import storage mount identity changed; review the route")
    # The current legacy entry can reconcile itself. Other legacy entries must
    # get a first observation before their unknown costs can be safely ignored.
    # Do not gate legacy entries on one another, which would prevent recovery.
    if not row or row.observed_mounts:
        await require_observed_imports(db, excluding_entry=entry.id)
    pool = await db.get(DownloadCapacity, parent.id) if parent else None
    remaining = dict(pool.import_resources) if pool else {}
    moved = min(amount, remaining.get(key, 0)) if not row or not row.resources else 0
    if moved:
        remaining[key] -= moved
    reserved = await reserved_bytes(db, excluding_entry=entry.id)
    reserved[key] -= moved
    verify_space(observation, {key: amount}, reserved, await settings(db))
    if pool and moved:
        pool.import_resources = remaining
    if not row:
        row = ImportCapacity(entry_id=entry.id)
        db.add(row)
    row.resources, row.observed_mounts = {key: amount}, observation["roots"]


async def release_import(db, entry):
    parent = await import_parent(db, entry)
    await transaction_lock(db, LOCK)
    row = await db.get(ImportCapacity, entry.id)
    if row:
        if row.resources:
            await consumed(db)
        row.resources = {}
    if parent:
        await db.flush()
        inspections = [parent.inspection_id] if parent.inspection_id else []
        inspections.extend(
            await db.scalars(
                select(DownloadHandoff.inspection_id).where(DownloadHandoff.attempt_id == parent.id)
            )
        )
        inspections.extend(
            await db.scalars(
                select(AutomaticImport.inspection_id).where(AutomaticImport.attempt_id == parent.id)
            )
        )
        pending = await db.scalar(
            select(ImportEntry.id)
            .join(ImportRun)
            .join(FrozenImportPlan)
            .where(
                FrozenImportPlan.inspection_id.in_([i for i in inspections if i]),
                ImportEntry.published_at.is_(None),
                ImportEntry.state.not_in(["cancelled", "skipped"]),
            )
            .limit(1)
        )
        pool = await db.get(DownloadCapacity, parent.id)
        if pool and not pending and parent.state == "complete":
            pool.import_resources = {}


def remaining_after_landing(required, written):
    return max(MIB, required - max(0, int(written)))


async def note_landed(entry_id, required, written):
    async with session_factory()() as db, db.begin():
        await transaction_lock(db, LOCK)
        row = await db.get(ImportCapacity, entry_id, populate_existing=True)
        if not row or not row.resources:
            return
        key = next(iter(row.resources))
        updated = {key: remaining_after_landing(required, written)}
        if row.resources != updated:
            row.resources = updated
