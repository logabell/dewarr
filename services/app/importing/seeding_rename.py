"""Opt-in rename of a completed qBittorrent download into the library folder.

The seeding file and the library file are the same copy. This stays off unless
a library folder enables it.
"""

import asyncio
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy import select

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.qbittorrent import QbitClient, absolute_path
from app.db.models import (
    AcquisitionSelection,
    DownloadAttempt,
    FrozenImportPlan,
    ImportEntry,
    ImportRun,
    Integration,
)
from app.db.session import session_factory
from app.domain.download_attempts import attempt_tag
from app.domain.downloaders import mappings_current, relative_to
from app.importing.filesystem import InspectionError, beneath, digest, directory
from app.importing.naming import AUDIO, EBOOK
from app.importing.publication import (
    PublicationError,
    generated_files,
    load_rename_plan,
    object_id,
    private_staging,
    publication_lock,
    remember_rename_plan,
    verify_item_media,
)
from app.importing.storage import import_sources
from app.security import decrypt_secrets

MEDIA_SUFFIXES = AUDIO | EBOOK
UNSEEN_LIBRARY = (
    "qBittorrent cannot see this library folder at that path. "
    "Enter the folder qBittorrent uses for this library."
)
RESTORE_FAILED = (
    "qBittorrent moved this torrent and it could not be returned to the download folder. "
    "Review the torrent in qBittorrent before retrying."
)


def normalize_seeding_target(enabled: bool, client_path: str | None) -> tuple[bool, str | None]:
    if not enabled:
        return False, None
    if not client_path:
        raise ValueError("Enter the library folder qBittorrent uses")
    try:
        return True, absolute_path(client_path)
    except ValueError as error:
        raise ValueError("Enter an absolute library folder qBittorrent can use") from error


def inspected_path(spec, file) -> str:
    base = PurePosixPath(spec.source_root) / spec.source_relative
    if spec.source_kind == "file":
        return absolute_path(str(base))
    return absolute_path(str(base / file.source))


def worker_for(save_path: str, relative: str, mappings) -> str | None:
    full = absolute_path(f"{save_path.rstrip('/')}/{relative}")
    matches = []
    for mapping in mappings:
        inside = relative_to(full, mapping["download_root"])
        if inside is None:
            continue
        root = str(mapping["source_path"]).rstrip("/")
        matches.append(absolute_path(root if not inside else f"{root}/{inside}"))
    if len(matches) != 1:
        return None
    return matches[0]


def order_renames(pairs) -> list[list[str]]:
    pairs = list(pairs)
    pending = [[old, new] for old, new in pairs if old != new]
    current = {old for old, _ in pending}
    steps: list[list[str]] = []
    guard = 0
    while pending:
        guard += 1
        if guard > len(pairs) * 4 + 4:
            raise PublicationError("Could not order the torrent renames")
        ready, blocked = [], []
        for old, new in pending:
            if new in current and new != old:
                blocked.append([old, new])
            else:
                ready.append([old, new])
        if ready:
            for old, new in ready:
                steps.append([old, new])
                current.discard(old)
                current.add(new)
            pending = blocked
            continue
        old, new = blocked.pop(0)
        temp = f".dewarr-rename-{len(steps)}"
        while temp in current:
            temp += "x"
        steps.append([old, temp])
        current.discard(old)
        current.add(temp)
        blocked.append([temp, new])
        pending = blocked
    return steps


def file_suffix(relative: str) -> str:
    name = PurePosixPath(relative).name
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def padding_file(relative: str) -> bool:
    name = PurePosixPath(relative).name
    return name.startswith("_____padding_file") or name.endswith(".pad")


def name_tokens(relative: str) -> set[str]:
    return set(re.split(r"[^0-9a-z]+", PurePosixPath(relative).stem.casefold()))


def sample_file(relative: str) -> bool:
    return bool(name_tokens(relative) & {"sample", "preview"})


def booklet_file(relative: str) -> bool:
    return bool(name_tokens(relative) & {"booklet", "bonus", "extra", "extras", "supplement"})


def imported_media_bytes(spec) -> int:
    return sum(file.identity["size"] for file in spec.files)


def small_beside_import(size_bytes: int, spec) -> bool:
    """A short preview stays under a fifth of the imported book."""
    total = imported_media_bytes(spec)
    return total > 0 and size_bytes * 5 < total


def imports_pdf(spec) -> bool:
    return any(file_suffix(file.name) == "pdf" for file in spec.files)


def extra_book(relative: str, spec, size_bytes: int) -> bool:
    """A second book blocks the rename. A short sample or a booklet stays with the torrent."""
    if padding_file(relative):
        return False
    suffix = file_suffix(relative)
    if suffix in AUDIO and sample_file(relative) and small_beside_import(size_bytes, spec):
        return False
    if suffix not in MEDIA_SUFFIXES:
        return False
    if suffix == "pdf" and not imports_pdf(spec):
        if booklet_file(relative) or small_beside_import(size_bytes, spec):
            return False
    return True


def names_of(state) -> set[str]:
    return {item.relative_path for item in state.files}


def plan_renames(state, spec, mappings, client_root: str) -> dict:
    if state.auto_managed:
        raise PublicationError(
            "Turn off automatic torrent management in qBittorrent before renaming the seeding copy."
        )
    if not state.completed or not all(item.complete for item in state.files):
        raise PublicationError("qBittorrent has not finished this download")
    if any(file.name == ".torrent" for file in spec.files):
        raise PublicationError(
            "Choose library filenames that leave room for the torrent's own files"
        )
    client_root = absolute_path(client_root)
    by_worker = {inspected_path(spec, file): file for file in spec.files}
    targets = {str(PurePosixPath(spec.folder) / file.name): file for file in spec.files}
    extras_root = f"{spec.folder}/.torrent"
    assigned, incidentals, extra_books = {}, {}, []
    for item in state.files:
        current = item.relative_path
        if state.save_path == client_root and current in targets:
            assigned[current] = current
            continue
        if state.save_path == client_root and current.startswith(f"{extras_root}/"):
            incidentals[current] = current
            continue
        worker = worker_for(state.save_path, current, mappings)
        matched = by_worker.get(worker) if worker else None
        if matched is not None:
            new_relative = str(PurePosixPath(spec.folder) / matched.name)
            if new_relative in assigned:
                raise PublicationError("Two seeding files would use the same library name")
            assigned[new_relative] = current
            continue
        if extra_book(current, spec, item.size_bytes):
            extra_books.append(current)
            continue
        # A dot directory stays out of the Audiobookshelf scan.
        new_relative = f"{extras_root}/{current}"
        if new_relative in incidentals or new_relative in assigned:
            raise PublicationError("Two torrent files would use the same library path")
        incidentals[new_relative] = current
    if extra_books:
        raise PublicationError(
            "This torrent contains other books that are not part of this import. "
            "Use hardlink or copy so those seeding files stay in the download folder."
        )
    if set(assigned) != set(targets):
        raise PublicationError("qBittorrent is not seeding every file in this import")
    pairs = [(old, new) for new, old in assigned.items()]
    pairs.extend((old, new) for new, old in incidentals.items())
    return {
        "location": client_root,
        "previous_location": absolute_path(state.save_path),
        "renames": order_renames(pairs),
        "targets": sorted(targets),
        "incidentals": sorted(incidentals),
        "originals": sorted(names_of(state)),
    }


def tree_names(folder) -> list[str]:
    found = []
    for name in os.listdir(folder):
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
        except NotADirectoryError:
            found.append(name)
            continue
        except OSError as error:
            raise PublicationError(
                "The library folder contains a link Dewarr will not follow"
            ) from error
        else:
            try:
                found.extend(f"{name}/{nested}" for nested in tree_names(child))
            finally:
                os.close(child)
    return found


def assert_destination_available(spec, incidentals):
    allowed = {file.name for file in spec.files} | set(generated_files(spec))
    prefix = f"{spec.folder}/.torrent/"
    expected = {path.removeprefix(prefix) for path in incidentals}
    if any(not path.startswith(prefix) for path in incidentals):
        raise PublicationError("Torrent files could not be placed beside the book")
    with directory(spec.destination_root) as root:
        try:
            with beneath(root, spec.folder, folder=True) as leaf:
                names = set(os.listdir(leaf))
                foreign = set()
                if ".torrent" in names:
                    with beneath(leaf, ".torrent", folder=True) as extras:
                        foreign = set(tree_names(extras)) - expected
        except FileNotFoundError:
            return
    if names - allowed - {".torrent"} or foreign:
        raise PublicationError("The library folder already contains other files")


def assert_existing_library_file(spec, state, plan):
    """Refuse before qBittorrent is changed when the organized file is another copy."""
    location = absolute_path(plan["location"])
    finals = set(plan["targets"]) | set(plan["incidentals"])
    if state.save_path == location and finals <= names_of(state):
        return
    deadline = time.monotonic() + 60
    with directory(spec.destination_root) as root:
        try:
            with beneath(root, spec.folder, folder=True) as leaf:
                present = set(os.listdir(leaf))
                for file in spec.files:
                    if file.name not in present:
                        continue
                    with beneath(leaf, file.name) as handle:
                        info = os.fstat(handle)
                        same_bytes = (
                            info.st_size == file.identity["size"]
                            and digest(handle, deadline) == file.sha256
                        )
                        same_file = (
                            info.st_dev == file.identity["device"]
                            and info.st_ino == file.identity["inode"]
                        )
                        if not same_bytes:
                            raise PublicationError(
                                "The library folder already contains a different file. "
                                "Remove it before renaming the seeding copy."
                            )
                        if not same_file:
                            raise PublicationError(
                                "The library folder already contains another copy of this book. "
                                "Remove it before renaming the seeding copy."
                            )
        except FileNotFoundError:
            return


async def wait_for_location(client, external_id: str, location: str, seconds: float):
    deadline = time.monotonic() + seconds
    while True:
        state = await client.status(external_id)
        if state.state in {"error", "missingFiles"}:
            raise PublicationError("qBittorrent lost the torrent files while moving them")
        settled = state.state not in {"moving", "checkingUP", "checkingDL"}
        if state.save_path == location and settled:
            if not state.completed:
                raise PublicationError("qBittorrent has not finished this download")
            return state
        if time.monotonic() >= deadline:
            raise AdapterError(
                FailureKind.TIMEOUT, "qBittorrent has not finished moving the torrent."
            )
        await asyncio.sleep(0.5)


def refuse_managed(state):
    if state.auto_managed:
        raise PublicationError(
            "Turn off automatic torrent management in qBittorrent before renaming the seeding copy."
        )


async def apply_renames(client, state, steps):
    present = names_of(state)
    for old, new in steps:
        if old == new or (old not in present and new in present):
            present.discard(old)
            present.add(new)
            continue
        if old not in present:
            raise PublicationError("qBittorrent is missing a file that should still be seeding")
        applied = await client.rename_file(state.external_id, old, new)
        if not applied:
            state = await client.status(state.external_id)
            present = names_of(state)
            if old not in present and new in present:
                continue
            raise PublicationError("qBittorrent did not rename the seeding file")
        present.discard(old)
        present.add(new)
    return await client.status(state.external_id)


async def apply_plan(client, state, plan, *, seconds=120):
    refuse_managed(state)
    location = absolute_path(plan["location"])
    previous = absolute_path(plan["previous_location"])
    finals = set(plan["targets"]) | set(plan["incidentals"])
    if state.save_path == location and finals <= names_of(state):
        return state
    if state.save_path not in {previous, location}:
        raise PublicationError("qBittorrent is seeding this download from an unexpected folder")
    if state.save_path == location:
        if not await client.set_location(state.external_id, previous):
            raise PublicationError("qBittorrent did not return the torrent to the download folder")
        state = await wait_for_location(client, state.external_id, previous, seconds)
    state = await apply_renames(client, state, plan["renames"])
    if state.save_path != location:
        if not await client.set_location(state.external_id, location):
            raise PublicationError(
                "qBittorrent did not move the seeding copy into the library folder"
            )
        state = await wait_for_location(client, state.external_id, location, seconds)
    final = await client.status(state.external_id)
    if final.save_path != location or not finals <= names_of(final):
        raise PublicationError("qBittorrent has not finished renaming every seeding file")
    return final


async def restore_plan(client, external_id: str, plan, *, seconds=120):
    previous = absolute_path(plan["previous_location"])
    state = await client.status(external_id)
    refuse_managed(state)
    if state.save_path != previous:
        if not await client.set_location(external_id, previous):
            raise PublicationError("qBittorrent did not return the torrent to the download folder")
        state = await wait_for_location(client, external_id, previous, seconds)
    reverse = [[new, old] for old, new in reversed(plan["renames"])]
    state = await apply_renames(client, state, reverse)
    if state.save_path != previous or not set(plan["originals"]) <= names_of(state):
        raise PublicationError("qBittorrent could not return the torrent to the download folder")
    return state


def verify_placed(spec):
    deadline = time.monotonic() + 60
    try:
        with directory(spec.destination_root) as root:
            with beneath(root, spec.folder, folder=True) as leaf:
                verify_item_media(leaf, spec, deadline)
    except FileNotFoundError as error:
        raise PublicationError(
            "qBittorrent has not placed the renamed files in the library folder"
        ) from error


async def confirm_library_bytes(spec, checks: int):
    last = None
    for attempt in range(checks):
        if attempt:
            await asyncio.sleep(0.4)
        try:
            await asyncio.to_thread(verify_placed, spec)
            return
        except PublicationError as error:
            text = str(error)
            if "not the seeding copy" in text or "does not match the downloaded bytes" in text:
                raise
            if not any(part in text for part in ("has not placed", "has not finished renaming")):
                raise
            last = error
    raise last


async def confirm_client_library(client, client_path: str, worker_root):
    """Prove qBittorrent's library path and Dewarr's folder are the same directory."""
    client_path = absolute_path(client_path)
    name = ".dewarr-route-" + uuid4().hex

    def create():
        with directory(worker_root) as root:
            os.mkdir(name, 0o700, dir_fd=root)

    def remove():
        with directory(worker_root) as root:
            os.rmdir(name, dir_fd=root)

    await asyncio.to_thread(create)
    try:
        try:
            entries = await client.directory_entries(client_path)
        except AdapterError as error:
            if error.kind is FailureKind.NOT_FOUND:
                raise PublicationError(UNSEEN_LIBRARY) from error
            raise
        if not isinstance(entries, list) or name not in entries:
            raise PublicationError(UNSEEN_LIBRARY)
    finally:
        try:
            await asyncio.to_thread(remove)
        except FileNotFoundError:
            pass


async def confirm_library_mapping(downloader_id, client_path, worker_root, *, client_factory=None):
    client_factory = client_factory or QbitClient
    async with session_factory()() as db:
        downloader = await db.get(Integration, UUID(str(downloader_id)))
        if (
            not downloader
            or downloader.kind != "qbittorrent"
            or not downloader.enabled
            or downloader.status != "connected"
        ):
            raise PublicationError("Connect qBittorrent before renaming the seeding copy")
        endpoint = downloader.base_url
        credentials = decrypt_secrets(downloader.encrypted_secrets)
    async with client_factory(endpoint, credentials["username"], credentials["password"]) as client:
        await confirm_client_library(client, client_path, worker_root)


async def commit_seeding_plan(client, state, plan, spec, *, seconds=120, checks=8):
    await asyncio.to_thread(assert_destination_available, spec, plan["incidentals"])
    await asyncio.to_thread(assert_existing_library_file, spec, state, plan)
    refuse_managed(state)
    location = absolute_path(plan["location"])
    previous = absolute_path(plan["previous_location"])
    if state.save_path not in {previous, location}:
        raise PublicationError("qBittorrent is seeding this download from an unexpected folder")
    try:
        await apply_plan(client, state, plan, seconds=seconds)
        await confirm_library_bytes(spec, checks)
    except (PublicationError, AdapterError, OSError) as error:
        try:
            await restore_plan(client, state.external_id, plan, seconds=seconds)
        except (PublicationError, AdapterError, OSError) as restore_error:
            raise PublicationError(RESTORE_FAILED) from restore_error
        raise error


def seeding_media_present(spec) -> bool:
    """True when every imported file is already a regular file in the library folder."""
    names = [file.name for file in spec.files]
    if not names:
        return False
    try:
        with directory(spec.destination_root) as root:
            with beneath(root, spec.folder, folder=True) as leaf:
                for name in names:
                    try:
                        with beneath(leaf, name):
                            continue
                    except (FileNotFoundError, InspectionError):
                        return False
                return True
    except FileNotFoundError:
        return False


@contextmanager
def seeding_lock(spec):
    """The same library lock publication uses, held across the qBittorrent rename."""
    with (
        private_staging(spec.staging_root, spec.journal_root) as staging,
        directory(spec.destination_root) as root,
    ):
        with publication_lock(staging, json.dumps(object_id(root), sort_keys=True)):
            yield


async def publish_still_owns(entry_id, token) -> bool:
    async with session_factory()() as db:
        entry = await db.get(ImportEntry, entry_id)
        return bool(entry and entry.run_token == token and entry.state == "publishing")


async def seeding_connection(entry_id):
    async with session_factory()() as db:
        entry = await db.get(ImportEntry, entry_id)
        run = await db.get(ImportRun, entry.run_id) if entry else None
        plan_row = await db.get(FrozenImportPlan, run.plan_id) if run else None
        attempt = None
        if plan_row:
            attempt = await db.scalar(
                select(DownloadAttempt).where(
                    DownloadAttempt.inspection_id == plan_row.inspection_id
                )
            )
        if not entry or not attempt:
            raise PublicationError(
                "Renaming the seeding copy applies to a completed qBittorrent download"
            )
        selection = await db.get(AcquisitionSelection, attempt.selection_id)
        downloader = await db.get(Integration, selection.downloader_id) if selection else None
        if (
            not downloader
            or downloader.kind != "qbittorrent"
            or not downloader.enabled
            or downloader.status != "connected"
        ):
            raise PublicationError("Connect qBittorrent before renaming the seeding copy")
        if not mappings_current(downloader, await import_sources(db)):
            raise PublicationError("Review the download path map before renaming the seeding copy")
        destination = entry.configuration["destination"]
        client_root = destination.get("client_path")
        if not destination.get("seeding_rename") or not client_root:
            raise PublicationError("Enter the library folder qBittorrent uses")
        mappings = list(downloader.config["mappings"])
        tag = attempt_tag(attempt)
        descriptor = selection.frozen["descriptor"]
        hashes = [
            value
            for value in (descriptor.get("infohash_v1"), descriptor.get("infohash_v2"))
            if value
        ]
        endpoint = downloader.base_url
        credentials = decrypt_secrets(downloader.encrypted_secrets)
    if not hashes:
        raise PublicationError("qBittorrent is not seeding this download")
    return {
        "endpoint": endpoint,
        "username": credentials["username"],
        "password": credentials["password"],
        "mappings": mappings,
        "tag": tag,
        "torrent_hash": hashes[0],
        "client_root": client_root,
    }


async def seeding_state(client, connection):
    states = await client.find(
        attempt_tag=connection["tag"], torrent_hash=connection["torrent_hash"]
    )
    if len(states) != 1:
        raise PublicationError(
            "More than one qBittorrent transfer matches this download"
            if states
            else "qBittorrent is not seeding this download"
        )
    return states[0]


async def restore_unplaced_rename(client, state, plan, spec, *, seconds=120):
    """Return download names when the library folder does not yet contain the book."""
    if await asyncio.to_thread(seeding_media_present, spec):
        return
    try:
        await restore_plan(client, state.external_id, plan, seconds=seconds)
    except (AdapterError, OSError) as error:
        raise PublicationError(RESTORE_FAILED) from error


async def undo_unplaced_rename(entry_id, spec, *, client_factory=None):
    if spec.mode != "rename":
        return
    plan = await asyncio.to_thread(load_rename_plan, spec)
    if not plan:
        return
    connection = await seeding_connection(entry_id)
    client_factory = client_factory or QbitClient
    with seeding_lock(spec):
        if await asyncio.to_thread(seeding_media_present, spec):
            return
        try:
            async with client_factory(
                connection["endpoint"], connection["username"], connection["password"]
            ) as client:
                state = await seeding_state(client, connection)
                await restore_unplaced_rename(client, state, plan, spec)
        except AdapterError as error:
            raise PublicationError(RESTORE_FAILED) from error


async def place_seeding_copy(entry_id, spec, token, *, client_factory=None):
    """Move and rename the seeding torrent, then leave publication to verify it."""
    from app.importing.execution import Superseded

    client_factory = client_factory or QbitClient
    connection = await seeding_connection(entry_id)
    stored = await asyncio.to_thread(load_rename_plan, spec)
    async with client_factory(
        connection["endpoint"], connection["username"], connection["password"]
    ) as client:
        state = await seeding_state(client, connection)
        plan = stored or plan_renames(
            state, spec, connection["mappings"], connection["client_root"]
        )
        if stored is None:
            # The journal is saved before the lock. Remembering also takes that lock.
            await asyncio.to_thread(remember_rename_plan, spec, plan)
        with seeding_lock(spec):
            if not await publish_still_owns(entry_id, token):
                raise Superseded("A newer import attempt owns this entry")
            state = await seeding_state(client, connection)
            await commit_seeding_plan(client, state, plan, spec)
