"""Bounded, additive ebook placement after normal import confirmation.

The canonical ebook stays intact. Every extra file has a durable database intent
before publication, and publication never overwrites existing library contents.
"""

import asyncio
import errno
import json
import os
import time
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from uuid import UUID

from sqlalchemy import delete, select

from app.config import get_settings
from app.db.models import (
    EbookCompanion,
    ImportDestination,
    ImportEntry,
    ImportRun,
    Integration,
    InventoryItemState,
    Library,
    LibraryAsset,
    OrganizationSettings,
    User,
    Version,
)
from app.db.session import session_factory
from app.domain.operations import transaction_lock
from app.domain.work_graph import canonical_work, family_ids, graph_lock
from app.importing.destinations import destination_configuration
from app.importing.filesystem import beneath, digest, directory, identity, relative_parts
from app.importing.naming import EBOOK
from app.importing.publication import (
    PublicationBusy,
    PublicationError,
    no_replace,
    object_id,
    private_staging,
    publication_lock,
    sync_directory,
    write_all,
)
from app.importing.settings import current_profile


class PlacementBudgetExpired(PublicationError):
    pass


def _check_budget(deadline):
    if time.monotonic() >= deadline:
        raise PlacementBudgetExpired("Ebook placement exceeded its time budget; review this file")


def _relative(path, root):
    value = str(PurePosixPath(path).relative_to(PurePosixPath(root)))
    relative_parts(value)
    return value


def _evidence(fd, *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 120
    _check_budget(deadline)
    before = identity(os.fstat(fd))
    try:
        sha = digest(fd, deadline)
    except ValueError:
        _check_budget(deadline)
        raise
    after = identity(os.fstat(fd))
    if before != after:
        raise PublicationError("Ebook changed while reading it")
    return {**after, "sha256": sha}


def source_evidence(configuration, *, deadline=None):
    with directory(Path(configuration["source"]["root_path"])) as root:
        with beneath(root, configuration["source_relative"]) as original:
            return _evidence(original, deadline=deadline)


def target_folder_identity(configuration):
    with directory(Path(configuration["target"]["root_path"])) as root:
        with beneath(
            root, str(PurePosixPath(configuration["target_relative"]).parent), folder=True
        ) as folder:
            return object_id(folder)


def verify_companion(configuration, receipt, *, deadline=None):
    with directory(Path(configuration["target"]["root_path"])) as root:
        with beneath(root, configuration["target_relative"]) as placed:
            actual = _evidence(placed, deadline=deadline)
    if any(
        actual.get(key) != receipt.get(key)
        for key in ("sha256", "size", "device", "inode", "mtime_ns")
    ):
        raise PublicationError("The tracked ebook copy changed; review it before reusing it")
    return actual


def place_companion(
    identifier, configuration, expected, *, checkpoint=lambda _: None, deadline=None
):
    """Resume an intent by checksum, or publish a complete no-replace file."""
    deadline = deadline if deadline is not None else time.monotonic() + 120
    _check_budget(deadline)
    target = configuration["target"]
    relative = PurePosixPath(configuration["target_relative"])
    temporary = f".dewarr-ebook-{identifier}.tmp"
    with (
        directory(Path(target["root_path"])) as root,
        private_staging(
            Path(target["staging_path"]),
            Path(target["journal_path"]) if target.get("journal_path") else None,
        ) as staging,
        publication_lock(staging, json.dumps(object_id(root), sort_keys=True)),
        beneath(root, str(relative.parent), folder=True) as folder,
    ):
        if object_id(folder) != configuration["target_folder_identity"]:
            raise PublicationError("The audiobook folder changed after ebook placement was planned")
        try:
            with beneath(folder, relative.name) as existing:
                observed = _evidence(existing, deadline=deadline)
            if observed["sha256"] != expected["sha256"] or observed["size"] != expected["size"]:
                raise PublicationError(
                    "An existing ebook differs from the tracked placement; it was left untouched"
                )
            return observed
        except FileNotFoundError:
            pass
        with (
            directory(Path(configuration["source"]["root_path"])) as source,
            beneath(source, configuration["source_relative"]) as original,
        ):
            observed = identity(os.fstat(original))
            if any(
                observed[key] != expected[key] for key in ("size", "device", "inode", "mtime_ns")
            ):
                raise PublicationError("The source ebook changed after placement was planned")
            try:
                with beneath(folder, temporary) as staged:
                    staged_evidence = _evidence(staged, deadline=deadline)
                if staged_evidence["sha256"] != expected["sha256"]:
                    raise PublicationError("An incomplete ebook staging file needs review")
            except FileNotFoundError:
                source_path = PurePosixPath(configuration["source_relative"])
                with (
                    nullcontext(source)
                    if str(source_path.parent) == "."
                    else beneath(source, str(source_path.parent), folder=True)
                ) as parent:
                    try:
                        os.link(
                            source_path.name,
                            temporary,
                            src_dir_fd=parent,
                            dst_dir_fd=folder,
                            follow_symlinks=False,
                        )
                    except OSError as error:
                        if error.errno not in {
                            errno.EXDEV,
                            errno.EPERM,
                            errno.EACCES,
                            errno.EOPNOTSUPP,
                            errno.ENOSYS,
                            errno.EMLINK,
                        }:
                            raise
                        output = os.open(
                            temporary,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o666,
                            dir_fd=folder,
                        )
                        try:
                            os.lseek(original, 0, os.SEEK_SET)
                            while block := os.read(original, 1024 * 1024):
                                _check_budget(deadline)
                                write_all(output, block)
                            os.fsync(output)
                        finally:
                            os.close(output)
                with beneath(folder, temporary) as staged:
                    staged_evidence = _evidence(staged, deadline=deadline)
                if (
                    staged_evidence["sha256"] != expected["sha256"]
                    or staged_evidence["size"] != expected["size"]
                ):
                    raise PublicationError("The staged ebook does not match the original") from None
            sync_directory(folder)
            checkpoint("staged")
            _check_budget(deadline)
            no_replace(folder, temporary, folder, relative.name)
            sync_directory(folder)
            checkpoint("published")
            with beneath(folder, relative.name) as placed:
                final = identity(os.fstat(placed))
                if any(
                    final[key] != staged_evidence[key]
                    for key in ("size", "device", "inode", "mtime_ns")
                ):
                    raise PublicationError("The ebook changed during publication")
                return {**final, "sha256": staged_evidence["sha256"]}


async def enqueue_for_import(db, entry, library, integration):
    if integration.kind != "audiobookshelf" or not (await current_profile(db)).ebooks_with_audio:
        return
    from app.jobs.queue import enqueue

    await enqueue(
        db,
        "organization.ebook-companions",
        entry_id=str(entry.id),
        job_lock=f"ebook-companions:{library.id}",
    )


async def _context(db, entry_id):
    if get_settings().recovery_mode:
        return None
    await graph_lock(db)
    await db.get(OrganizationSettings, 1, with_for_update={"read": True})
    if not (await current_profile(db)).ebooks_with_audio:
        return None
    entry = await db.get(ImportEntry, entry_id)
    if not entry or entry.state != "confirmed":
        return None
    run = await db.get(ImportRun, entry.run_id)
    actor = await db.get(User, run.owner_id, with_for_update={"read": True})
    if not actor or not actor.active or actor.role != "admin":
        return None
    destination = await db.get(ImportDestination, entry.destination_id)
    library = await db.get(Library, destination.library_id)
    # Census and ordinary imports lock the integration before the library.
    # Resolve the parent without a row lock, then refresh the library after
    # acquiring its parent so a concurrent reassignment cannot reuse stale scope.
    integration = await db.get(Integration, library.integration_id, with_for_update={"read": True})
    library = await db.get(
        Library, destination.library_id, with_for_update=True, populate_existing=True
    )
    if (
        library.integration_id != integration.id
        or not library.accessible
        or not integration.enabled
        or integration.kind != "audiobookshelf"
    ):
        return None
    version = await db.get(Version, entry.version_id)
    work = await canonical_work(db, version.work_id)
    await transaction_lock(db, f"ebook-companions:{library.id}:{work.id}")
    routes = list(
        await db.scalars(
            select(ImportDestination)
            .where(
                ImportDestination.library_id == library.id,
                ImportDestination.enabled.is_(True),
                ImportDestination.deleted_at.is_(None),
                ImportDestination.workflow == "library",
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    )
    from app.importing.destination_view import view

    configurations = {
        str(route.id): await destination_configuration(db, route)
        for route in routes
        if (await view(db, route)).publication_available
    }
    return entry, library, integration, work, configurations


def _route(configurations, medium, path):
    matches = []
    for key, config in configurations.items():
        if (
            config["medium"] != medium
            or not config.get("root_path")
            or not config.get("staging_path")
        ):
            continue
        try:
            relative = _relative(path, config["backend_path"])
        except ValueError:
            continue
        matches.append((key, config, relative))
    return matches[0] if len(matches) == 1 else None


async def reconcile(entry_id, after=None):
    """At most twenty files per job; further narration folders continue durably."""
    entry_id = UUID(str(entry_id))
    deadline = time.monotonic() + 120
    resume = False
    async with session_factory()() as db, db.begin():
        context = await _context(db, entry_id)
        if not context:
            return
        entry, library, integration, work, configurations = context
        # Publication can commit before its scan fails. Already placed copies
        # still need scanning if their canonical source or route later goes away.
        scan_needed = bool(
            await db.scalar(
                select(EbookCompanion.id)
                .join(LibraryAsset, LibraryAsset.id == EbookCompanion.target_asset_id)
                .join(Version, Version.id == LibraryAsset.version_id)
                .where(
                    EbookCompanion.library_id == library.id,
                    EbookCompanion.state == "present",
                    LibraryAsset.library_id == library.id,
                    LibraryAsset.medium == "audio",
                    Version.work_id.in_(family_ids(work.id)),
                )
                .limit(1)
            )
        )
        assets = (
            select(LibraryAsset)
            .join(Version, Version.id == LibraryAsset.version_id)
            .where(
                LibraryAsset.library_id == library.id,
                LibraryAsset.state == "present",
                LibraryAsset.full_content.is_(True),
                LibraryAsset.containment.is_(None),
                LibraryAsset.match_status == "matched",
                Version.work_id.in_(family_ids(work.id)),
            )
        )
        # Prefer the canonical, standalone ebook. Do not copy copies recursively.
        audios = assets.where(LibraryAsset.medium == "audio")
        audio_external = select(LibraryAsset.external_id).where(
            LibraryAsset.library_id == library.id, LibraryAsset.medium == "audio"
        )
        source = await db.scalar(
            assets.where(
                LibraryAsset.medium == "ebook", LibraryAsset.external_id.not_in(audio_external)
            )
            .order_by(LibraryAsset.created_at, LibraryAsset.id)
            .limit(1)
        )
        source_files = (
            [file for file in source.files if file.get("format") in EBOOK]
            if source and not source.metadata_snapshot.get("ebook_supplementary")
            else []
        )
        if source and (
            not source_files
            or len(source_files) > len(EBOOK)
            or len({file["format"] for file in source_files}) != len(source_files)
        ):
            entry.message = (
                "Available; ebook placement needs review: choose one complete file per ebook format"
            )
            source_files = []
        if not source_files and not scan_needed:
            return
        page_size = max(1, 20 // (len(source_files) or 1))
        if after:
            audios = audios.where(LibraryAsset.id > UUID(after))
        targets = (
            list(await db.scalars(audios.order_by(LibraryAsset.id).limit(page_size + 1)))
            if source_files
            else []
        )
        placements = []
        source_cache = {}
        for target_asset in targets[:page_size]:
            if resume:
                break
            folder = target_asset.metadata_snapshot.get("path")
            if not folder or not (target_route := _route(configurations, "audio", folder)):
                continue
            for file in source_files:
                if time.monotonic() >= deadline:
                    resume = True
                    break
                if file.get("format") not in EBOOK or not (
                    source_route := _route(configurations, "ebook", file["path"])
                ):
                    continue
                source_id, source_config, source_relative = source_route
                target_id, target_config, target_folder = target_route
                target_path = str(PurePosixPath(folder) / PurePosixPath(file["path"]).name)
                row = await db.scalar(
                    select(EbookCompanion).where(
                        EbookCompanion.target_asset_id == target_asset.id,
                        EbookCompanion.target_path == target_path,
                    )
                )
                if row:
                    if row.version_id != source.version_id or row.state == "present":
                        continue
                    if row.state == "held" and row.receipt.get("budget_retry"):
                        continue
                    if row.receipt.get("source"):
                        placements.append(row.id)
                        continue
                config = {
                    "source_destination_id": source_id,
                    "target_destination_id": target_id,
                    "source": source_config,
                    "target": target_config,
                    "source_relative": source_relative,
                    "target_relative": str(
                        PurePosixPath(target_folder) / PurePosixPath(file["path"]).name
                    ),
                }
                evidence, problem = None, None
                try:
                    config["target_folder_identity"] = await asyncio.to_thread(
                        target_folder_identity, config
                    )
                    if file["path"] not in source_cache:
                        source_cache[file["path"]] = await asyncio.to_thread(
                            source_evidence, config, deadline=deadline
                        )
                    evidence = source_cache[file["path"]]
                    if evidence["size"] != file["size"]:
                        raise PublicationError(
                            "Source ebook size differs from the library observation"
                        )
                except (OSError, ValueError) as error:
                    problem = str(error)[:400]
                    entry.message = "Available; ebook placement needs attention: " + problem
                    resume = isinstance(error, PlacementBudgetExpired)
                if not row:
                    row = EbookCompanion(
                        library_id=library.id,
                        source_asset_id=source.id,
                        target_asset_id=target_asset.id,
                        version_id=source.version_id,
                        source_path=file["path"],
                        target_path=target_path,
                    )
                retry_budget = resume and not (row.receipt or {}).get("budget_retry")
                row.configuration = config
                row.receipt = (
                    {"budget_retry": True}
                    if resume
                    else {"source": evidence}
                    if evidence and not problem
                    else {}
                )
                row.state = "pending" if retry_budget or not problem else "held"
                row.message = (
                    "Waiting for a fresh ebook placement budget"
                    if retry_budget
                    else problem or "Waiting to place ebook"
                )
                db.add(row)
                await db.flush()
                if not problem:
                    placements.append(row.id)
                if resume:
                    break
        more = str(targets[page_size - 1].id) if len(targets) > page_size else None
    for identifier in placements:
        if time.monotonic() >= deadline:
            resume = True
            break
        if await _place(entry_id, identifier, deadline=deadline) == "budget":
            resume = True
            break
    async with session_factory()() as db, db.begin():
        context = await _context(db, entry_id)
        if not context:
            return
        _, library, integration, _, _ = context
        from app.jobs.queue import enqueue

        if resume or more:
            await enqueue(
                db,
                "organization.ebook-companions",
                entry_id=str(entry_id),
                after=after if resume else more,
                job_lock=f"ebook-companions:{library.id}",
            )
        # Force fresh item detail on the next census, including supplementary ebooks.
        await db.execute(
            delete(InventoryItemState).where(
                InventoryItemState.integration_id == integration.id,
                InventoryItemState.library_external_id == library.external_id,
            )
        )
        external, base_url, secrets = (
            library.external_id,
            integration.base_url,
            integration.encrypted_secrets,
        )
    if placements or scan_needed:
        from app.adapters.audiobookshelf import Audiobookshelf
        from app.security import decrypt_secrets

        async with Audiobookshelf(base_url, decrypt_secrets(secrets)["token"]) as adapter:
            await adapter.scan(external)


async def _place(entry_id, identifier, *, deadline=None):
    async with session_factory()() as db, db.begin():
        context = await _context(db, entry_id)
        if not context:
            return
        entry, _, _, work, configurations = context
        row = await db.get(EbookCompanion, identifier, with_for_update=True)
        config = row.configuration
        if any(
            configurations.get(config[key + "_destination_id"]) != config[key]
            for key in ("source", "target")
        ):
            row.state, row.message = "held", "Library routing changed; review ebook placement"
            return
        source = await db.get(LibraryAsset, row.source_asset_id, with_for_update={"read": True})
        target = await db.get(LibraryAsset, row.target_asset_id, with_for_update={"read": True})
        source_version = await db.get(Version, row.version_id, with_for_update={"read": True})
        target_version = (
            await db.get(Version, target.version_id, with_for_update={"read": True})
            if target
            else None
        )
        if (
            not source
            or not target
            or not source_version
            or not target_version
            or source.state != "present"
            or target.state != "present"
            or source.match_status != "matched"
            or target.match_status != "matched"
            or not source.full_content
            or not target.full_content
            or source.medium != "ebook"
            or target.medium != "audio"
            or source.library_id != row.library_id
            or target.library_id != row.library_id
            or source.containment
            or target.containment
            or target.metadata_snapshot.get("path") != str(PurePosixPath(row.target_path).parent)
            or not any(
                file.get("path") == row.source_path
                and file.get("size") == row.receipt["source"]["size"]
                for file in source.files
            )
            or source.version_id != row.version_id
            or (await canonical_work(db, source_version.work_id)).id != work.id
            or (await canonical_work(db, target_version.work_id)).id != work.id
        ):
            row.state, row.message = (
                "held",
                "Source ebook or audiobook identity changed; review placement",
            )
            return
        task = asyncio.create_task(
            asyncio.to_thread(
                place_companion, row.id, config, row.receipt["source"], deadline=deadline
            )
        )
        try:
            receipt = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        except PublicationBusy:
            # Ordinary imports share this filesystem lock. Their temporary
            # contention must reach the job's bounded retry policy.
            raise
        except PlacementBudgetExpired as error:
            retry_budget = not row.receipt.get("budget_retry")
            row.receipt = {**row.receipt, "budget_retry": True}
            row.state = "pending" if retry_budget else "held"
            row.message = (
                "Waiting for a fresh ebook placement budget" if retry_budget else str(error)
            )
            if not retry_budget:
                entry.message = "Available; ebook placement needs attention: " + str(error)
            return "budget"
        except (OSError, ValueError) as error:
            row.state, row.message = "held", str(error)[:500]
            entry.message = "Available; ebook placement needs attention: " + str(error)[:400]
        else:
            row.receipt = {**row.receipt, **receipt}
            row.state, row.message = "present", "Ebook placed in audiobook folder"
