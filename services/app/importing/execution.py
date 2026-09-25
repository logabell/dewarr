"""Durable item publication and independent ABS confirmation.

The final rename holds permission/configuration/attempt rows until it finishes.
Bulk file work happens outside database transactions in private staging.
"""

import asyncio
import base64
import hashlib
import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.grimmory import FORMAT_TYPES, Grimmory
from app.config import get_settings
from app.db.models import (
    AuditEvent,
    FrozenImportPlan,
    ImportDestination,
    ImportEntry,
    ImportRun,
    Integration,
    Library,
    LibraryAsset,
    Operation,
    ProviderObject,
    User,
    Version,
)
from app.db.session import session_factory
from app.domain import capacity, download_reviews
from app.domain.catalog_language import catalog_language
from app.domain.identity import normalized
from app.domain.inventory import apply_item
from app.importing.backend import verify_backend
from app.importing.collection_contents import verify as verify_contents
from app.importing.covers import CoverError, fetch_cover
from app.importing.destinations import destination_configuration
from app.importing.failures import import_failure
from app.importing.filesystem import beneath, digest, directory
from app.importing.naming import AUDIO, EBOOK
from app.importing.ownership import already_owned
from app.importing.publication import (
    PublicationBusy,
    PublicationError,
    PublicationSpec,
    publish_item,
)
from app.importing.storage import import_sources
from app.importing.versioning import version_revision
from app.security import decrypt_secrets

logger = logging.getLogger(__name__)


class Superseded(PublicationError):
    pass


class AlreadyOwned(PublicationError):
    pass


async def context(db, entry, token, *, lock=False):
    run = await db.get(ImportRun, entry.run_id)
    plan = await db.get(FrozenImportPlan, run.plan_id)
    if lock:
        await download_reviews.lock_principals(db, plan.inspection_id)
    destination = await db.get(ImportDestination, entry.destination_id)
    backend = entry.configuration["destination"]["backend"]
    actor = await db.get(
        User, run.owner_id, with_for_update={"read": True} if lock else None, populate_existing=True
    )
    integration = await db.get(
        Integration, UUID(backend["integration_id"]), with_for_update=lock, populate_existing=True
    )
    library = await db.get(
        Library, destination.library_id, with_for_update=lock, populate_existing=True
    )
    if lock:
        await db.refresh(destination, with_for_update=True)
        await db.refresh(entry, with_for_update=True)
    if entry.run_token != token or entry.state not in {"publishing", "awaiting-library"}:
        raise Superseded("A newer import attempt owns this entry")
    if (
        get_settings().recovery_mode
        or not actor
        or not actor.active
        or actor.role != "admin"
        or not integration.enabled
        or not library.accessible
        or not destination.enabled
    ):
        raise PublicationError("Import access changed or recovery mode is active")
    if await destination_configuration(db, destination) != entry.configuration["destination"]:
        raise PublicationError("Destination or connection changed; review it before retrying")
    if (
        str((await import_sources(db)).get(entry.configuration["source_key"]))
        != entry.configuration["source_path"]
    ):
        raise PublicationError("Source mapping changed; do not publish this frozen plan")
    version = await db.get(
        Version,
        entry.version_id,
        with_for_update={"read": True} if lock else None,
        populate_existing=True,
    )
    if version_revision(version) != entry.expected_metadata["version_revision"]:
        raise PublicationError("Catalog version identity changed; this import needs review")
    conflicts = await db.scalar(
        select(ProviderObject.id)
        .where(
            ProviderObject.version_id == version.id, ProviderObject.match_status == "needs-review"
        )
        .limit(1)
    )
    if conflicts:
        raise PublicationError("Resolve this catalog version's metadata conflict first")
    try:
        await verify_contents(db, entry.expected_metadata.get("collection_contents", []), lock=lock)
        from app.importing.automatic import publication_authority

        if not entry.published_at:
            await publication_authority(
                db, run.id, version=version, destination_id=destination.id, lock=lock
            )
        await download_reviews.validate_inspection(
            db, plan.inspection_id, destination_id=destination.id, version=version, lock=lock
        )
    except HTTPException as error:
        raise PublicationError(str(error.detail)) from error
    return run, destination, integration, library


class RenameGuard:
    def __init__(self, loop, entry_id, token, spec=None):
        self.loop, self.entry_id, self.token = loop, entry_id, token
        self.spec = spec
        self.db = None

    async def enter(self):
        observation = None
        if self.spec:
            observation = await capacity.observe_publication(self.spec)
            async with session_factory()() as db, db.begin():
                entry = await db.get(ImportEntry, self.entry_id)
                await context(db, entry, self.token, lock=True)
                await capacity.staged_import(db, entry, observation)
            observation = await capacity.observe_publication(self.spec)
        self.db = session_factory()()
        try:
            await self.db.begin()
            entry = await self.db.get(ImportEntry, self.entry_id)
            _, _, _, library = await context(self.db, entry, self.token, lock=True)
            run = await self.db.get(ImportRun, entry.run_id)
            plan = await self.db.get(FrozenImportPlan, run.plan_id)
            if await already_owned(
                self.db, entry.version_id, library.id, inspection_id=plan.inspection_id
            ):
                raise AlreadyOwned("This version became available before publication")
            if observation:
                await capacity.publication_capacity(self.db, entry, observation)
        except BaseException:
            await self.db.rollback()
            await self.db.close()
            self.db = None
            raise

    async def leave(self):
        if self.db:
            try:
                await self.db.rollback()  # Guard carries locks, never optimistic state writes.
            finally:
                await self.db.close()
                self.db = None

    @contextmanager
    def hold(self):
        # The event loop stays alive while the shielded filesystem thread finishes.
        asyncio.run_coroutine_threadsafe(self.enter(), self.loop).result()
        try:
            yield
        finally:
            asyncio.run_coroutine_threadsafe(self.leave(), self.loop).result()


def published_sizes(spec, receipt):
    sizes = {file.name: file.identity["size"] for file in spec.files}
    if spec.conversion:
        recorded = (receipt or {}).get("derived", {}).get(spec.conversion.output_name)
        if not recorded or "size" not in recorded:
            raise PublicationError("Converted audiobook has no recorded size")
        sizes[spec.conversion.output_name] = recorded["size"]
    return sizes


def verify_published_media(spec, receipt=None):
    with directory(spec.destination_root) as root, beneath(root, spec.folder, folder=True) as item:
        derived = (receipt or {}).get("derived") or {}
        if spec.conversion:
            recorded = derived.get(spec.conversion.output_name)
            with beneath(item, spec.conversion.output_name) as media:
                if not recorded or digest(media, time.monotonic() + 300) != recorded["sha256"]:
                    raise PublicationError(
                        "Converted audiobook changed; library confirmation is held"
                    )
        for file in spec.files:
            with beneath(item, file.name) as media:
                if digest(media, time.monotonic() + 300) != file.sha256:
                    raise PublicationError("Published media changed; library confirmation is held")


def _size_matches(expected_bytes: int, file) -> bool:
    if getattr(file, "size_unit", "byte") != "kilobyte":
        return file.size == expected_bytes
    return abs(file.size - expected_bytes) < 1024


def detection_needs_another_scan(kind, capabilities, *, published_now, found) -> bool:
    """Grimmory ignores a refresh that arrives while one is already running.

    A later confirmation asks again when the book is still absent and folder watch
    is not there to notice it. The publish attempt already requested the first refresh.
    """
    return (
        kind == "grimmory"
        and not found
        and not published_now
        and bool(capabilities.get("scan_capable"))
        and not capabilities.get("watcher_enabled")
    )


def _app_name(item) -> str:
    if str(getattr(item, "cover_path", "") or "").startswith("grimmory:"):
        return "Grimmory"
    return "Audiobookshelf"


def _same_sequence(expected, actual) -> bool:
    if not expected:
        return True
    if actual is None:
        return False
    if str(actual) == str(expected):
        return True
    try:
        return float(expected) == float(actual)
    except (TypeError, ValueError):
        return False


def _same_files(entry, item) -> bool:
    config, metadata = entry.configuration["destination"], entry.expected_metadata
    folder = str(PurePosixPath(config["backend_path"]) / entry.specification["folder"])
    if item.path != folder:
        return False
    spec = PublicationSpec.model_validate(entry.specification)
    selected = {
        str(PurePosixPath(folder) / name): size
        for name, size in published_sizes(spec, getattr(entry, "receipt", None)).items()
    }
    media = {file.path: file for file in item.library_files if file.format in AUDIO | EBOOK}
    if (
        set(media) != set(selected)
        or any(not _size_matches(selected[path], file) for path, file in media.items())
        or item.missing
        or item.invalid
        or getattr(item, "unreadable", False)
        or not getattr(item, "full_" + metadata["medium"])
    ):
        raise PublicationError(
            "Library item boundaries or media files differ from the frozen import"
        )
    return True


def _same_narrators(expected, actual, *, grimmory: bool) -> bool:
    if sorted(map(normalized, expected)) == sorted(map(normalized, actual)):
        return True
    # Grimmory keeps one string, so a comma can belong to a name. The written
    # form is ", ".join, and that still matches after a comma split.
    return grimmory and normalized(", ".join(expected)) == normalized(", ".join(actual))


def _same_metadata(entry, item, *, playback_order: bool = True) -> None:
    metadata = entry.expected_metadata
    library = _app_name(item)
    if normalized(item.title) != normalized(metadata["title"]):
        raise PublicationError(f"{library} title differs from the exported title")
    if playback_order and (expected_order := metadata.get("audio_order")):
        indices = [file.playback_index for file in item.audio]
        if (
            any(index is None for index in indices)
            or len(set(indices)) != len(indices)
            or [file.path for file in sorted(item.audio, key=lambda file: file.playback_index)]
            != expected_order
        ):
            raise PublicationError(
                f"{library} playback order differs from the reviewed disc and track order"
            )
    for key in ("authors", "narrators"):
        expected = metadata[key] if key == "authors" or metadata["medium"] == "audio" else []
        if not expected:
            continue
        actual = getattr(item, key)
        same = (
            _same_narrators(expected, actual, grimmory=library == "Grimmory")
            if key == "narrators"
            else sorted(map(normalized, expected)) == sorted(map(normalized, actual))
        )
        if not same:
            raise PublicationError(f"{library} {key} differ from the exported metadata")
    year = metadata["edition_year" if metadata["medium"] == "ebook" else "recording_year"]
    if year and year != item.year:
        raise PublicationError(f"{library} publication year differs from the exported version")
    if metadata.get("language") and catalog_language(metadata["language"]) != catalog_language(
        item.language or ""
    ):
        raise PublicationError(f"{library} language differs from the exported version")
    if metadata.get("series") and not any(
        series.get("name") == metadata["series"]
        and _same_sequence(metadata.get("sequence"), series.get("sequence"))
        for series in item.series
    ):
        raise PublicationError(f"{library} series metadata differs from the export")


def _published_names(entry) -> dict[str, int]:
    spec = PublicationSpec.model_validate(entry.specification)
    return {file.name: file.identity["size"] for file in spec.files}


def _media_files(item):
    return [file for file in item.library_files if file.format in AUDIO | EBOOK]


def _published_folder(entry) -> str:
    return str(
        PurePosixPath(entry.configuration["destination"]["backend_path"])
        / entry.specification["folder"]
    )


def _grimmory_ready(entry, item) -> bool:
    metadata = entry.expected_metadata
    return not (
        item.missing
        or item.invalid
        or getattr(item, "unreadable", False)
        or not getattr(item, "full_" + metadata["medium"])
    )


def _grimmory_same_names(entry, item) -> bool:
    """Same folder and filenames after Grimmory rewrites a file in place."""
    if item.path != _published_folder(entry) or not _grimmory_ready(entry, item):
        return False
    actual = {PurePosixPath(file.path).name for file in _media_files(item)}
    return actual == set(_published_names(entry))


def _grimmory_same_sizes(entry, item) -> bool:
    """Same file count and sizes after Grimmory renames a book onto its library pattern."""
    if not _grimmory_ready(entry, item):
        return False
    expected = list(_published_names(entry).values())
    remaining = _media_files(item)
    if len(remaining) != len(expected):
        return False
    for size in expected:
        match = next((file for file in remaining if _size_matches(size, file)), None)
        if match is None:
            return False
        remaining.remove(match)
    return True


def grimmory_relocated_match(entry, item) -> bool:
    """A journaled book Grimmory moved. File size still has to match.

    Extension alone is not enough: the published file may have been deleted
    while another copy of the same edition remains.
    """
    try:
        return matches(entry, item)
    except PublicationError:
        return False


def published_file(item, name, identity, used, *, grimmory: bool):
    """The library file for one published name, including a Grimmory pattern rename."""
    direct = next(
        (
            file
            for file in item.library_files
            if file not in used and file.path == str(PurePosixPath(item.path or "") / name)
        ),
        None,
    )
    if direct is not None:
        return direct
    if not grimmory:
        return None
    extension = PurePosixPath(name).suffix.lower().lstrip(".")
    return next(
        (
            file
            for file in _media_files(item)
            if file not in used
            and (not extension or file.format == extension)
            and _size_matches(identity["size"], file)
        ),
        None,
    )


def matches(entry, item):
    grimmory = _app_name(item) == "Grimmory"
    try:
        same_folder = _same_files(entry, item)
    except PublicationError:
        # A metadata rewrite can change the byte size while leaving the filename.
        if not (grimmory and _grimmory_same_names(entry, item)):
            raise
        same_folder = True
    if not same_folder and not (grimmory and _grimmory_same_sizes(entry, item)):
        return False
    # Grimmory orders tracks by filename and may rename them after metadata is saved.
    _same_metadata(entry, item, playback_order=not grimmory)
    return True


def verified_ebook_files(entry, item, *, grimmory: bool) -> list[dict]:
    """Ebook files confirmed for this import.

    Audiobookshelf exposes one primary file, so extra formats stay tied to the
    published paths. Grimmory's library pattern renames those paths; the files
    it reports now are the ones this import placed.
    """
    if grimmory:
        published = {
            PurePosixPath(file["name"]).suffix.lower().lstrip(".")
            for file in entry.specification["files"]
        }
        selected = [
            file for file in item.library_files if file.format in published and file.format in EBOOK
        ]
    else:
        paths = set(
            entry.expected_metadata.get("ebook_media_paths") or [file.path for file in item.ebook]
        )
        selected = [
            file for file in item.library_files if file.path in paths and file.format in EBOOK
        ]
    return [{**file.model_dump(), "import_verified": True} for file in selected]


async def prepare_cover(entry_id, token):
    async with session_factory()() as db:
        entry = await db.get(ImportEntry, entry_id)
        await context(db, entry, token)
        source = entry.expected_metadata.get("cover_source")
        if entry.cover_export is not None or not source or entry.published_at:
            return PublicationSpec.model_validate(entry.specification)
    try:
        content = await fetch_cover(source)
        cover = {
            "state": "prepared",
            "message": "Selected catalog cover prepared for initial export",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    except CoverError as error:
        content = None
        cover = {"state": "unavailable", "message": f"No cover exported: {error}"}
    async with session_factory()() as db, db.begin():
        current = await db.get(ImportEntry, entry_id)
        run, _, _, _ = await context(db, current, token, lock=True)
        spec = PublicationSpec.model_validate(current.specification)
        if current.cover_export is None:
            if content:
                spec = PublicationSpec.model_validate(
                    {
                        **spec.model_dump(mode="json"),
                        "binary_sidecars": {"cover.jpg": base64.b64encode(content).decode("ascii")},
                    }
                )
            current.specification = spec.model_dump(mode="json")
            current.cover_export = cover
            db.add(
                AuditEvent(
                    actor_id=run.owner_id,
                    action="organization.cover.prepared",
                    entity_id=current.id,
                    detail={"state": cover["state"], "sha256": cover.get("sha256")},
                )
            )
        return spec


def observe_cover(spec):
    if not spec.binary_sidecars:
        return None
    try:
        with (
            directory(spec.destination_root) as root,
            beneath(root, spec.folder, folder=True) as item,
            beneath(item, "cover.jpg") as file,
        ):
            return digest(file, time.monotonic() + 15)
    except (OSError, ValueError):
        return None  # External artwork changes do not invalidate the book's media.


async def find_item(adapter, entry, library_external_id):
    page, expected_total, found = 0, None, []
    while True:
        rows, total = await adapter.page(library_external_id, page)
        if expected_total is not None and total != expected_total:
            raise PublicationError("Library inventory changed during confirmation; retry detection")
        expected_total = total
        for item in await adapter.expanded([row["id"] for row in rows]) if rows else []:
            if item.library_id != library_external_id:
                raise PublicationError(f"{_app_name(item)} item moved during confirmation")
            if getattr(item, "unreadable", False):
                continue
            if _same_files(entry, item):
                found.append(item)
        if (page + 1) * adapter.page_size >= total:
            break
        page += 1
    if len(found) > 1:
        raise PublicationError(
            f"{_app_name(found[0])} reports duplicate items for this import folder"
        )
    if not found:
        return None
    current = await adapter.item(found[0].id)
    if current.library_id != library_external_id or not _same_files(entry, current):
        raise PublicationError(f"{_app_name(current)} item changed during confirmation")
    if hasattr(adapter, "apply_catalog_metadata"):
        await adapter.apply_catalog_metadata(
            current.id, entry.expected_metadata, observed_year=current.year
        )
        current = await adapter.item(current.id)
        if current.library_id != library_external_id:
            raise PublicationError(f"{_app_name(current)} item changed during confirmation")
        # Grimmory may write those fields into the file or move it onto its library
        # pattern, and it orders folder tracks by filename. The book was already
        # identified by its files; keep its new location and its own playback order.
        _same_metadata(entry, current, playback_order=False)
        return current
    if not matches(entry, current):
        raise PublicationError(f"{_app_name(current)} item changed during confirmation")
    return current


async def finish_state(entry_id, token, state, message, *, receipt=None):
    async with session_factory()() as db, db.begin():
        entry = await db.get(ImportEntry, entry_id, with_for_update=True)
        if entry.run_token != token:
            return
        operation = await db.get(Operation, entry.operation_id)
        entry.state, entry.message, entry.run_token = state, message, None
        if receipt:
            entry.receipt = receipt
        if state == "skipped":
            entry.reserved = False
            await capacity.release_import(db, entry)
        entry.next_check_at = (
            datetime.now(UTC) + timedelta(minutes=1)
            if state in {"awaiting-library", "queued"}
            else None
        )
        operation.status = (
            "completed"
            if state in {"confirmed", "skipped", "awaiting-library"}
            else "queued"
            if state == "queued"
            else "failed"
        )
        operation.message = message


async def confirm_observation(db, current, integration, library, item, observed_cover, actor_id):
    """Record an already verified item within the caller's transaction.

    Callers must verify current identity, route, collection, physical files and ABS
    evidence before entering this helper. It neither publishes nor queues work.
    """
    spec = PublicationSpec.model_validate(current.specification)
    version = await db.get(Version, current.version_id)
    namespace = (
        f"grimmory:{integration.id}" if integration.kind == "grimmory" else f"abs:{integration.id}"
    )
    library_name = "Grimmory" if integration.kind == "grimmory" else "Audiobookshelf"
    link = await db.scalar(
        select(ProviderObject)
        .where(
            ProviderObject.provider == namespace,
            ProviderObject.kind == f"item:{version.medium}",
            ProviderObject.external_id == item.id,
        )
        .with_for_update()
    )
    if link and link.manual_lock and link.version_id != version.id:
        raise PublicationError("A manual library match conflicts with this imported version")
    if not link:
        link = ProviderObject(
            provider=namespace, kind=f"item:{version.medium}", external_id=item.id
        )
        db.add(link)
    link.work_id, link.version_id, link.manual_lock, link.match_status = (
        version.work_id,
        version.id,
        True,
        "matched",
    )
    link.snapshot = item.model_dump(mode="json")
    await db.flush()
    await apply_item(db, library, item, library.generation, integration.id, {item.id})
    await db.flush()
    asset = await db.scalar(
        select(LibraryAsset).where(
            LibraryAsset.library_id == library.id,
            LibraryAsset.external_id == item.id,
            LibraryAsset.medium == version.medium,
        )
    )
    if not asset or not asset.full_content or asset.version_id != version.id:
        raise PublicationError(
            f"{library_name} observation did not produce the intended full library asset"
        )
    if version.medium == "ebook":
        asset.files = verified_ebook_files(current, item, grimmory=integration.kind == "grimmory")
    current.asset_id, current.confirmed_at = asset.id, datetime.now(UTC)
    contents = current.expected_metadata.get("collection_contents", [])
    if contents:
        from app.domain.containment import review as accept_contents
        from app.domain.corrections import asset_state, revision

        try:
            await accept_contents(
                db,
                actor_id,
                asset.id,
                [UUID(book["work_id"]) for book in contents],
                revision(await asset_state(db, asset, link)),
                physical_version=version,
            )
        except HTTPException as error:
            raise PublicationError(str(error.detail)) from error
    if current.cover_export and current.cover_export["state"] == "prepared":
        expected_cover = str(
            PurePosixPath(current.configuration["destination"]["backend_path"])
            / spec.folder
            / "cover.jpg"
        )
        unchanged = observed_cover == current.cover_export["sha256"]
        # Grimmory's folder scan uses cover.jpg in the book folder and does not
        # report that path back. An unchanged export is the cover it can select.
        selected = item.cover_path == expected_cover or (
            integration.kind == "grimmory" and unchanged
        )
        current.cover_export = {
            **current.cover_export,
            "backend_selected": selected,
            "unchanged": unchanged,
            "message": f"Selected cover detected in {library_name}"
            if selected and unchanged
            else (
                f"Artwork changed or {library_name} selected another cover; "
                "the initial export will not overwrite it"
            ),
        }
    current.state, current.message, current.run_token, current.next_check_at = (
        "confirmed",
        f"Available in {library_name}",
        None,
        None,
    )
    return version, contents, asset


def _still_publishing(loop, entry_id, token):
    def should_continue():
        try:
            asyncio.run_coroutine_threadsafe(_require_publishing(entry_id, token), loop).result(
                timeout=10
            )
        except Superseded:
            raise
        except Exception:
            return

    return should_continue


async def _require_publishing(entry_id, token):
    async with session_factory()() as db:
        entry = await db.get(ImportEntry, entry_id)
        if entry is None or entry.run_token != token or entry.state != "publishing":
            raise Superseded("This import was cancelled or superseded")


def _note_progress(loop, entry_id, required):
    def on_progress(written):
        try:
            asyncio.run_coroutine_threadsafe(
                capacity.note_landed(entry_id, required, written), loop
            ).result(timeout=10)
        except Exception:
            return

    return on_progress


async def execute(operation_id: UUID, *, client_factory=None, checkpoint=lambda _: None):
    token = uuid4()
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id)
        if not operation or operation.kind != "organization.publish":
            return
        entry = await db.get(ImportEntry, UUID(operation.payload["entry_id"]), with_for_update=True)
        if entry.state in {"confirmed", "skipped", "held", "cancel-held", "cancelled"}:
            return
        cancelling = entry.state == "cancelling"
        entry_id = entry.id
        if not cancelling:
            entry.run_token = token
            entry.state = "awaiting-library" if entry.published_at else "publishing"
            operation.status, operation.message = "running", "Checking this book's import state"
    if cancelling:
        from app.importing.cancellation import execute as cancel

        await cancel(operation_id, checkpoint=checkpoint)
        return
    stage = "Checking import configuration"
    try:
        async with session_factory()() as db:
            entry = await db.get(ImportEntry, entry_id)
            _, _, integration, library = await context(db, entry, token)
            secrets = decrypt_secrets(integration.encrypted_secrets)
            secret = secrets if integration.kind == "grimmory" else secrets["token"]
            url, external_library = integration.base_url, library.external_id
            factory = client_factory or (
                Grimmory if integration.kind == "grimmory" else Audiobookshelf
            )
            library_name = "Grimmory" if integration.kind == "grimmory" else "Audiobookshelf"
            spec = PublicationSpec.model_validate(entry.specification)
            receipt = entry.receipt
        async with factory(url, secret) as adapter:
            stage = "Connecting to the library"
            capabilities = await verify_backend(
                adapter,
                external_library,
                entry.configuration["destination"]["backend_path"],
                spec.destination_root,
                entry.expected_metadata["medium"],
            )
            if integration.kind == "grimmory":
                allowed = set(
                    (capabilities.get("configuration") or {}).get("allowed_formats") or []
                )
                for media in spec.files:
                    extension = PurePosixPath(media.name).suffix.lower().lstrip(".")
                    book_type = FORMAT_TYPES.get(extension)
                    if book_type is None:
                        raise PublicationError(
                            "Grimmory indexes EPUB, PDF, MOBI, AZW3, FB2, CBZ, CBR, CB7, "
                            "M4B, M4A, MP3, and Opus. This file uses another format."
                        )
                    if allowed and book_type not in allowed:
                        raise PublicationError(
                            f"This Grimmory library does not accept {book_type} files."
                        )
            published_now = not entry.published_at
            if published_now:
                stage = "Preparing artwork"
                spec = await prepare_cover(entry_id, token)
                entry.specification = spec.model_dump(mode="json")
                stage = "Checking library storage"
                observation = await capacity.observe_import(spec)
                async with session_factory()() as db, db.begin():
                    current = await db.get(ImportEntry, entry_id)
                    await context(db, current, token, lock=True)
                    await capacity.reconcile_import(db, current, observation)
                observation = {
                    **await capacity.observe_publication(spec),
                    "required_bytes": observation["required_bytes"],
                }
                async with session_factory()() as db, db.begin():
                    current = await db.get(ImportEntry, entry_id)
                    await context(db, current, token, lock=True)
                    await capacity.reserve_import(db, current, spec, observation)
                stage = "Organizing library files"
                if spec.mode == "rename":
                    from app.importing.seeding_rename import place_seeding_copy

                    await place_seeding_copy(entry_id, spec, token)
                loop = asyncio.get_running_loop()
                required = observation["required_bytes"]
                guard = RenameGuard(loop, entry_id, token, spec)
                timeout = 12 * 3600 if spec.conversion else 600
                task = asyncio.create_task(
                    asyncio.to_thread(
                        publish_item,
                        spec,
                        checkpoint=checkpoint,
                        timeout=timeout,
                        publication_guard=guard.hold,
                        should_continue=(
                            _still_publishing(loop, entry_id, token) if spec.conversion else None
                        ),
                        on_progress=(
                            _note_progress(loop, entry_id, required) if spec.conversion else None
                        ),
                    )
                )
                try:
                    receipt = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
                checkpoint("published-before-database")
                entry.receipt = receipt
                async with session_factory()() as db, db.begin():
                    current = await db.get(ImportEntry, entry_id, with_for_update=True)
                    if current.run_token != token:
                        return
                    current.receipt, current.published_at = receipt, datetime.now(UTC)
                    current.state, current.message = (
                        "awaiting-library",
                        f"Published; waiting for {library_name} to confirm the item",
                    )
                    current.next_check_at = datetime.now(UTC) + timedelta(minutes=1)
                    await capacity.release_import(db, current)
                    db.add(
                        AuditEvent(
                            actor_id=operation.owner_id,
                            action="organization.item.published",
                            entity_id=current.id,
                        )
                    )
            stage = "Confirming the library copy"
            if capabilities["scan_capable"] and published_now:
                await adapter.scan(external_library)
            await asyncio.to_thread(verify_published_media, spec, receipt)
            item = await find_item(adapter, entry, external_library)
            if item is None and capabilities["scan_capable"] and not published_now:
                await adapter.scan(external_library)
                item = await find_item(adapter, entry, external_library)
            if detection_needs_another_scan(
                integration.kind,
                capabilities,
                published_now=published_now,
                found=item is not None,
            ):
                await adapter.scan(external_library)
                item = await find_item(adapter, entry, external_library)
            if item is None:
                async with session_factory()() as db:
                    current = await db.get(ImportEntry, entry_id)
                    overdue = current.published_at and datetime.now(
                        UTC
                    ) - current.published_at > timedelta(minutes=30)
                await finish_state(
                    entry_id,
                    token,
                    "held" if overdue else "awaiting-library",
                    (
                        f"{library_name} has not detected the expected item; "
                        "check the library and retry detection"
                        if overdue
                        else f"Published; waiting for {library_name} to detect the complete item"
                    ),
                )
                return
            observed_cover = await asyncio.to_thread(observe_cover, spec)
            async with session_factory()() as db, db.begin():
                current = await db.get(ImportEntry, entry_id)
                _, _, integration, library = await context(db, current, token, lock=True)
                version, contents, asset = await confirm_observation(
                    db, current, integration, library, item, observed_cover, operation.owner_id
                )
                stored_operation = await db.get(Operation, operation_id)
                stored_operation.status, stored_operation.message = "completed", current.message
                # Enqueue in this transaction; the reconciler acquires work locks
                # afterward, never in reverse order under import publication locks.
                from app.jobs.queue import enqueue

                await enqueue(db, "acquisition.fulfillment", work_id=str(version.work_id))
                for book in contents:
                    await enqueue(db, "acquisition.fulfillment", work_id=book["work_id"])
                db.add(
                    AuditEvent(
                        actor_id=operation.owner_id,
                        action="organization.item.confirmed",
                        entity_id=current.id,
                        detail={"asset_id": str(asset.id)},
                    )
                )
    except Superseded:
        return
    except AlreadyOwned as error:
        await finish_state(entry_id, token, "skipped", str(error))
    except capacity.CapacityWait as error:
        await finish_state(entry_id, token, "queued", str(error))
    except PublicationBusy as error:
        await finish_state(entry_id, token, "queued", f"{error}. This import will retry.")
    except (PublicationError, AdapterError, OSError, ValueError, InvalidToken, KeyError) as error:
        message = (
            str(error)[:500]
            if isinstance(error, (PublicationError, AdapterError))
            else import_failure(error, stage)
        )
        logger.warning(
            "Import %s failed during %s (%s, errno=%s)",
            entry_id,
            stage,
            type(error).__name__,
            getattr(error, "errno", None),
        )
        transient = isinstance(error, AdapterError) and error.kind in {
            FailureKind.UNAVAILABLE,
            FailureKind.TIMEOUT,
            FailureKind.RATE_LIMIT,
            FailureKind.UNCERTAIN,
        }
        async with session_factory()() as db:
            current = await db.get(ImportEntry, entry_id)
            waiting = bool(
                transient
                and current.published_at
                and datetime.now(UTC) - current.published_at < timedelta(minutes=30)
            )
        await finish_state(entry_id, token, "awaiting-library" if waiting else "held", message)
