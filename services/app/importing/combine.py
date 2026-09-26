"""Fold the separate part items of one recording into one Audiobookshelf book.

Parts stay in their own folders until every part is in the library. Their files are then
hard-linked into ``Book/Disc N`` in private staging and published with a no-replace rename.
The old part folders are removed only after Audiobookshelf reports the combined book with
exactly those files, and the journal keeps what is needed to separate the parts again.
"""

import asyncio
import errno
import json
import os
import stat
from collections import Counter
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import select

from app.adapters.audiobookshelf import Audiobookshelf
from app.adapters.contracts import AdapterError, FailureKind
from app.config import get_settings
from app.db.models import (
    AssetContains,
    AuditEvent,
    ImportDestination,
    ImportEntry,
    Integration,
    Library,
    LibraryAsset,
    Operation,
    PartCombine,
    ProviderObject,
    User,
    Version,
    Work,
)
from app.db.session import session_factory
from app.domain.catalog_titles import parse_title_labels
from app.importing.filesystem import beneath, directory, enumerate_files, identity, relative_parts
from app.importing.layout import library_relative, overlaps, unsafe_staging
from app.importing.naming import AUDIO, StrictModel, collision_key, fingerprint
from app.importing.publication import (
    HARDLINKS_UNSUPPORTED,
    PublicationError,
    conflicting_name,
    destination_parent,
    journal_fd,
    no_replace,
    object_id,
    private_staging,
    publication_lock,
    read_receipt,
    same_object,
    sync_directory,
    write_all,
    write_receipt,
)

TERMINAL = {"completed", "cancelled", "failed"}
ACTIVE_IMPORTS = ("queued", "publishing", "awaiting-library")
# Part metadata would make a scanner read the combined book as one part again. It is kept
# in private staging instead, so separating the parts puts it back.
PART_METADATA = {"metadata.opf", "metadata.json", "desc.txt", "reader.txt"}
PART_METADATA_SUFFIXES = {".opf", ".nfo"}
COVERS = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg")
# Audiobookshelf may store its own metadata in an item folder.
SCANNER_FILES = {"metadata.json", "metadata.abs"}
KEYS = ("device", "inode", "size", "mtime_ns")
NO_HARDLINKS = (
    "Combining needs hard links, with the staging folder on the same filesystem as the library"
)
CONFIRM_ATTEMPTS = 12
CONFIRM_DELAY = 5.0


class CombineSkipped(Exception):
    """This book can't be combined right now. The message says why, for the book page."""


class CombineError(PublicationError):
    pass


class CombineFile(StrictModel):
    # Relative to the destination root.
    source: str
    # Relative to the combined book folder (media and cover) or to the archive (metadata).
    target: str
    identity: dict[str, int]


class CombinePart(StrictModel):
    index: int = Field(ge=1, le=20)
    total: int = Field(ge=2, le=20)
    asset_id: UUID
    item_id: str
    # The part's folder, or its single file, relative to the destination root.
    source: str
    kind: Literal["directory", "file"]
    audio: dict[str, int]


class CombineSpec(StrictModel):
    combine_id: UUID
    destination_root: Path
    staging_root: Path
    journal_root: Path | None = None
    backend_path: str
    folder: str
    parts: list[CombinePart] = Field(min_length=2, max_length=20)
    files: list[CombineFile] = Field(min_length=1, max_length=10000)
    archived: list[CombineFile] = Field(default_factory=list, max_length=2000)
    cover: CombineFile | None = None
    sidecars: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def confined(self):
        for root in (self.destination_root, self.staging_root):
            if not root.is_absolute() or str(root) == "/" or ".." in root.parts:
                raise ValueError("Use absolute non-root paths")
        if unsafe_staging(self.destination_root, self.staging_root):
            raise ValueError("Library and staging roots must not overlap")
        if self.journal_root is not None:
            if (
                self.journal_root.anchor != "/"
                or str(self.journal_root) == "/"
                or ".." in self.journal_root.parts
            ):
                raise ValueError("Use an absolute non-root journal path")
            if any(
                overlaps(self.journal_root, root)
                for root in (self.destination_root, self.staging_root)
            ):
                raise ValueError("Journal storage must be separate from media roots")
        relative_parts(self.folder)
        library_relative(self.folder)
        discs = {f"Disc {part.index}" for part in self.parts}
        for file in [*self.files, *self.archived, *([self.cover] if self.cover else [])]:
            relative_parts(file.source)
            library_relative(file.source)
            relative_parts(file.target)
        for file in [*self.files, *self.archived]:
            if file.target.split("/")[0] not in discs:
                raise ValueError("Every part file belongs in its disc folder")
        for part in self.parts:
            relative_parts(part.source)
            library_relative(part.source)
            book = PurePosixPath(self.folder)
            source = PurePosixPath(part.source)
            if book == source or book.is_relative_to(source) or source.is_relative_to(book):
                raise ValueError("The combined folder cannot overlap a part folder")
        if set(self.sidecars) - {"metadata.opf"}:
            raise ValueError("Only a generated metadata.opf is written for the combined book")
        names = [file.target for file in self.files] + list(self.sidecars)
        if self.cover:
            if "/" in self.cover.target:
                raise ValueError("The book cover sits in the book folder")
            names.append(self.cover.target)
        if len({collision_key(name) for name in names}) != len(names):
            raise ValueError("Combined filenames collide")
        return self

    def backend(self, relative):
        return str(PurePosixPath(self.backend_path) / relative)

    def expected_audio(self):
        """Backend path and size of every audio file in the combined book."""
        return {
            self.backend(f"{self.folder}/{file.target}"): file.identity["size"]
            for file in self.files
            if PurePosixPath(file.target).suffix[1:].lower() in AUDIO
        }


def spec_hash(spec):
    payload = spec.model_dump(mode="json")
    if spec.journal_root is None:
        payload.pop("journal_root")
    return fingerprint(payload)


def receipt_name(spec):
    return f"combine-{spec.combine_id}.json"


def stage_name(spec):
    return "combine-" + spec.combine_id.hex


def archive_name(spec):
    return "combine-archive-" + spec.combine_id.hex


def same_file(info, expected):
    current = identity(info)
    return all(current[key] == expected[key] for key in KEYS)


def same_inode(info, expected):
    return (info.st_dev, info.st_ino, info.st_size) == (
        expected["device"],
        expected["inode"],
        expected["size"],
    )


def lstat(fd, name):
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


@contextmanager
def opened_parent(root, relative, *, create=False):
    """The directory holding ``relative`` beneath ``root``, and the leaf name."""
    parts = relative_parts(relative)
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o777, dir_fd=fd)
                    sync_directory(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def link_file(source_root, source, target_root, target, expected):
    """Hard-link one verified file. Never replaces a name another file already holds."""
    with (
        opened_parent(source_root, source) as (origin, origin_name),
        opened_parent(target_root, target, create=True) as (parent, name),
    ):
        info = lstat(origin, origin_name)
        if info is None or not stat.S_ISREG(info.st_mode) or not same_file(info, expected):
            raise CombineError(f"{source} changed since the combine was planned")
        existing = lstat(parent, name)
        if existing is not None:
            if (existing.st_dev, existing.st_ino) == (info.st_dev, info.st_ino):
                return
            raise CombineError(f"{target} is already taken")
        try:
            os.link(origin_name, name, src_dir_fd=origin, dst_dir_fd=parent, follow_symlinks=False)
        except OSError as error:
            if error.errno in HARDLINKS_UNSUPPORTED | {errno.EXDEV}:
                raise CombineSkipped(NO_HARDLINKS) from error
            raise
        sync_directory(parent)


def write_new(root, relative, content: bytes):
    with opened_parent(root, relative, create=True) as (parent, name):
        existing = lstat(parent, name)
        if existing is not None:
            with beneath(parent, name) as fd, os.fdopen(os.dup(fd), "rb") as stream:
                if stream.read() == content:
                    return
            raise CombineError(f"{relative} is already taken")
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=parent
        )
        try:
            write_all(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_directory(parent)


def remove_tree(parent, name):
    """Remove a folder this module built. Only plain files and folders are expected."""
    info = lstat(parent, name)
    if info is None:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise CombineError("Unexpected entry in the combine staging folder")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        for entry in os.listdir(fd):
            child = lstat(fd, entry)
            if child and stat.S_ISDIR(child.st_mode):
                remove_tree(fd, entry)
            elif child and stat.S_ISREG(child.st_mode):
                os.unlink(entry, dir_fd=fd)
            elif child:
                raise CombineError("Unexpected entry in the combine staging folder")
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def folders_of(paths):
    """Every folder above these relative paths, not counting the top."""
    found = set()
    for path in paths:
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            found.add(str(parent))
            parent = parent.parent
    return found


def prune(root, relative, inner):
    """Remove the now-empty folders of a part, deepest first. True when the part is gone."""
    for folder in sorted(inner, key=lambda value: -value.count("/")):
        try:
            with opened_parent(root, f"{relative}/{folder}") as (parent, name):
                os.rmdir(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        except OSError as error:
            if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise
    try:
        with opened_parent(root, relative) as (parent, name):
            os.rmdir(name, dir_fd=parent)
            sync_directory(parent)
    except FileNotFoundError:
        return True
    except OSError as error:
        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return False
        raise
    return True


def survey(root: Path, part: dict):
    """What is on disk for one part: its kind and every file with its identity."""
    with directory(root) as root_fd:
        with opened_parent(root_fd, part["source"]) as (parent, name):
            info = lstat(parent, name)
        if info is None:
            raise CombineSkipped(f"Part {part['index']} is not in the mapped library folder")
        if stat.S_ISDIR(info.st_mode):
            with beneath(root_fd, part["source"], folder=True) as fd:
                return "directory", enumerate_files(fd)
        if stat.S_ISREG(info.st_mode):
            return "file", [(name, identity(info))]
    raise CombineSkipped(f"Part {part['index']} is not a plain folder or file")


def build_spec(context):
    """Freeze the folder plan from the parts on disk. Runs in a worker thread."""
    root = context["root"]
    parts, files, archived, cover = [], [], [], None
    for part in context["parts"]:
        kind, found = survey(root, part)
        index = part["index"]
        disc = f"Disc {index}"
        sizes = {name: value["size"] for name, value in found}
        audio = {}
        for path, size in part["audio"].items():
            name = (
                PurePosixPath(path).name
                if kind == "file"
                else str(
                    PurePosixPath(path).relative_to(
                        PurePosixPath(context["backend_path"]) / part["source"]
                    )
                )
            )
            if sizes.get(name) != size:
                raise CombineSkipped(
                    f"The files on disk for part {index} don't match what Audiobookshelf "
                    "reports; check the library folder mapping"
                )
            audio[path] = size
        media = [name for name, _ in found if PurePosixPath(name).suffix[1:].lower() in AUDIO]
        if not media:
            raise CombineSkipped(f"Part {index} has no audio files on disk")
        if any("/" in name for name in media):
            raise CombineSkipped(f"Part {index} already has its own disc folders")
        for name, value in found:
            source = part["source"] if kind == "file" else f"{part['source']}/{name}"
            entry = CombineFile(source=source, target=f"{disc}/{name}", identity=value)
            lowered = name.casefold()
            if "/" not in name and (
                lowered in PART_METADATA or PurePosixPath(lowered).suffix in PART_METADATA_SUFFIXES
            ):
                archived.append(entry)
                continue
            files.append(entry)
            if index == 1 and cover is None and "/" not in name and lowered in COVERS:
                cover = CombineFile(source=source, target=lowered, identity=value)
        parts.append(
            CombinePart(
                index=index,
                total=part["total"],
                asset_id=part["asset_id"],
                item_id=part["item_id"],
                source=part["source"],
                kind=kind,
                audio=audio,
            )
        )
    return CombineSpec(
        combine_id=context["combine_id"],
        destination_root=root,
        staging_root=context["staging"],
        journal_root=context.get("journals"),
        backend_path=context["backend_path"],
        folder=context["folder"],
        parts=sorted(parts, key=lambda part: part.index),
        files=files,
        archived=archived,
        cover=cover,
        sidecars=context["sidecars"],
    )


@contextmanager
def journal(spec):
    """Lock the library and open this combine's receipt in private staging."""
    with (
        private_staging(spec.staging_root, spec.journal_root) as staging,
        directory(spec.destination_root) as destination,
    ):
        if same_object(staging, object_id(destination)):
            raise CombineError("Staging and library refer to the same folder")
        with publication_lock(staging, json.dumps(object_id(destination), sort_keys=True)):
            receipt = read_receipt(staging, receipt_name(spec))
            if receipt is not None and (
                receipt.get("spec_hash") != spec_hash(spec)
                or receipt.get("destination_identity") != object_id(destination)
            ):
                raise CombineError("The combine journal no longer matches this library folder")
            yield staging, destination, receipt


def record(staging, spec, receipt, state):
    receipt["state"] = state
    write_receipt(staging, receipt_name(spec), receipt)


def verify_combined(destination, spec):
    with beneath(destination, spec.folder, folder=True) as leaf:
        for file in [*spec.files, *([spec.cover] if spec.cover else [])]:
            with opened_parent(leaf, file.target) as (parent, name):
                info = lstat(parent, name)
            if info is None or not same_inode(info, file.identity):
                raise CombineError(f"{spec.folder}/{file.target} is not the part's own file")
        for name, content in spec.sidecars.items():
            with beneath(leaf, name) as fd, os.fdopen(os.dup(fd), "rb") as stream:
                if stream.read() != content.encode():
                    raise CombineError(f"{spec.folder}/{name} changed after it was written")


def publish_combined(spec, *, checkpoint=lambda _: None):
    """Build ``Book/Disc N`` from hard links in staging, then rename it into the library."""
    with journal(spec) as (staging, destination, receipt):
        if receipt is None:
            receipt = {
                "schema_version": 1,
                "combine_id": str(spec.combine_id),
                "spec_hash": spec_hash(spec),
                "state": "preparing",
                "destination_identity": object_id(destination),
            }
            write_receipt(staging, receipt_name(spec), receipt, create=True)
        if receipt["state"] not in {"preparing", "staged"}:
            verify_combined(destination, spec)
            return receipt
        try:
            with beneath(destination, spec.folder, folder=True) as existing:
                ours = bool(receipt.get("stage_identity")) and same_object(
                    existing, receipt["stage_identity"]
                )
        except FileNotFoundError:
            ours = None
        if ours is False:
            raise CombineSkipped(f"The folder {spec.folder} already exists in the library")
        if ours:
            verify_combined(destination, spec)
            record(staging, spec, receipt, "published")
            return receipt
        # A stage left by an interrupted attempt holds only links and generated metadata.
        remove_tree(staging, stage_name(spec))
        os.mkdir(stage_name(spec), mode=0o777, dir_fd=staging)
        with beneath(staging, stage_name(spec), folder=True) as stage:
            receipt["stage_identity"] = object_id(stage)
            write_receipt(staging, receipt_name(spec), receipt)
            for file in [*spec.files, *([spec.cover] if spec.cover else [])]:
                link_file(destination, file.source, stage, file.target, file.identity)
            for name, content in spec.sidecars.items():
                write_new(stage, name, content.encode())
            sync_directory(stage)
        record(staging, spec, receipt, "staged")
        checkpoint("staged")
        with destination_parent(destination, spec.folder) as (parent, leaf):
            if conflicting_name(parent, leaf) is not None:
                raise CombineSkipped(f"The folder {spec.folder} already exists in the library")
            with directory(spec.destination_root) as current:
                if not same_object(current, receipt["destination_identity"]):
                    raise CombineError("The library folder changed before publication")
            with beneath(staging, stage_name(spec), folder=True) as current:
                if not same_object(current, receipt["stage_identity"]):
                    raise CombineError("The staged book changed before publication")
            no_replace(staging, stage_name(spec), parent, leaf)
            sync_directory(parent)
            sync_directory(staging)
        checkpoint("published-before-receipt")
        verify_combined(destination, spec)
        record(staging, spec, receipt, "published")
        return receipt


def discard(spec):
    """Forget a combine that never reached the library. Nothing in the library changes."""
    with journal(spec) as (staging, destination, receipt):
        if receipt and receipt["state"] not in {"preparing", "staged"}:
            raise CombineError("The combined book is already in the library")
        try:
            with beneath(destination, spec.folder, folder=True) as existing:
                if (
                    receipt
                    and receipt.get("stage_identity")
                    and same_object(existing, receipt["stage_identity"])
                ):
                    raise CombineError("The combined book is already in the library")
        except FileNotFoundError:
            pass
        remove_tree(staging, stage_name(spec))
        try:
            os.unlink(receipt_name(spec), dir_fd=journal_fd(staging))
        except FileNotFoundError:
            pass
        sync_directory(journal_fd(staging))


def mark(spec, state):
    with journal(spec) as (staging, _, receipt):
        if receipt is None:
            raise CombineError("The combine journal is missing from staging")
        record(staging, spec, receipt, state)


def journal_state(spec):
    with journal(spec) as (_, _, receipt):
        return receipt["state"] if receipt else None


def cleanup_parts(spec):
    """Remove the part files the combined book now holds. Returns parts left in place.

    Each original is removed only while the combined book holds that same file, and only
    if nobody changed it after the plan. Part metadata moves to the staging archive.
    """
    left = []
    with journal(spec) as (staging, destination, receipt):
        if receipt is None or receipt["state"] not in {"handed-over", "cleaned"}:
            raise CombineError("The combined book isn't confirmed yet")
        verify_combined(destination, spec)
        try:
            os.mkdir(archive_name(spec), mode=0o700, dir_fd=staging)
        except FileExistsError:
            pass
        with (
            beneath(staging, archive_name(spec), folder=True) as archive,
            beneath(destination, spec.folder, folder=True) as leaf,
        ):
            changed = set()
            for file in spec.archived:
                with opened_parent(destination, file.source) as (parent, name):
                    info = lstat(parent, name)
                    if info is None:
                        continue
                    if not same_file(info, file.identity):
                        changed.add(file.source)
                        continue
                link_file(destination, file.source, archive, file.target, file.identity)
                with opened_parent(destination, file.source) as (parent, name):
                    os.unlink(name, dir_fd=parent)
            for file in spec.files:
                with opened_parent(destination, file.source) as (parent, name):
                    info = lstat(parent, name)
                    if info is None:
                        continue
                    with opened_parent(leaf, file.target) as (held, held_name):
                        kept = lstat(held, held_name)
                    if (
                        not same_file(info, file.identity)
                        or kept is None
                        or (kept.st_dev, kept.st_ino) != (info.st_dev, info.st_ino)
                    ):
                        changed.add(file.source)
                        continue
                    os.unlink(name, dir_fd=parent)
                    sync_directory(parent)
        for part in spec.parts:
            if part.kind == "file":
                if part.source in changed:
                    left.append(part.source)
                continue
            inner = folders_of(
                str(PurePosixPath(file.source).relative_to(part.source))
                for file in [*spec.files, *spec.archived]
                if PurePosixPath(file.source).is_relative_to(part.source)
            )
            if not prune(destination, part.source, inner):
                left.append(part.source)
        record(staging, spec, receipt, "cleaned")
    return left


def restore_parts(spec):
    """Put every part back in its original place, from the combined book and the archive."""
    with journal(spec) as (staging, destination, receipt):
        if receipt is None or receipt["state"] not in {
            "cleaned",
            "done",
            "restoring",
            "restored",
        }:
            raise CombineError("Only a finished combine can be separated")
        verify_combined(destination, spec)
        record(staging, spec, receipt, "restoring")
        try:
            archive = os.open(
                archive_name(spec), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=staging
            )
        except FileNotFoundError:
            archive = None
        try:
            with beneath(destination, spec.folder, folder=True) as leaf:
                for part in spec.parts:
                    disc = f"Disc {part.index}/"
                    members = [
                        (leaf, file) for file in spec.files if file.target.startswith(disc)
                    ] + [
                        (archive, file)
                        for file in spec.archived
                        if file.target.startswith(disc) and archive is not None
                    ]
                    if part.kind == "file":
                        for origin, file in members:
                            link_back(origin, file, destination, file.source)
                        continue
                    with destination_parent(destination, part.source) as (parent, name):
                        exists = lstat(parent, name) is not None
                    if exists:
                        for origin, file in members:
                            link_back(origin, file, destination, file.source)
                        continue
                    temporary = f"separate-{spec.combine_id.hex}-{part.index}"
                    remove_tree(staging, temporary)
                    os.mkdir(temporary, mode=0o777, dir_fd=staging)
                    with beneath(staging, temporary, folder=True) as stage:
                        for origin, file in members:
                            relative = str(PurePosixPath(file.source).relative_to(part.source))
                            link_back(origin, file, stage, relative)
                    with destination_parent(destination, part.source) as (parent, name):
                        no_replace(staging, temporary, parent, name)
                        sync_directory(parent)
        finally:
            if archive is not None:
                os.close(archive)
        record(staging, spec, receipt, "restored")


def link_back(origin, file, target_root, target):
    with opened_parent(origin, file.target) as (parent, name):
        info = lstat(parent, name)
    if info is None or not same_inode(info, file.identity):
        raise CombineError(f"{file.target} changed after the parts were combined")
    expected = identity(info)
    link_file(origin, file.target, target_root, target, expected)


def remove_combined(spec):
    """Remove the combined book once every part is back. Returns False if anything was left."""
    with journal(spec) as (staging, destination, receipt):
        if receipt is None or receipt["state"] not in {"separated", "removed"}:
            raise CombineError("The parts aren't confirmed back in the library yet")
        for file in [*spec.files, *spec.archived]:
            with opened_parent(destination, file.source) as (parent, name):
                info = lstat(parent, name)
            if info is None or not same_inode(info, file.identity):
                raise CombineError(f"{file.source} isn't back in place")
        clean = True
        try:
            with beneath(destination, spec.folder, folder=True) as leaf:
                for file in [*spec.files, *([spec.cover] if spec.cover else [])]:
                    with opened_parent(leaf, file.target) as (parent, name):
                        info = lstat(parent, name)
                        if info is None:
                            continue
                        if same_inode(info, file.identity):
                            os.unlink(name, dir_fd=parent)
                        else:
                            clean = False
                for name, content in spec.sidecars.items():
                    info = lstat(leaf, name)
                    if info is None:
                        continue
                    with beneath(leaf, name) as fd, os.fdopen(os.dup(fd), "rb") as stream:
                        same = stream.read() == content.encode()
                    if same:
                        os.unlink(name, dir_fd=leaf)
                    else:
                        clean = False
                for name in SCANNER_FILES:
                    info = lstat(leaf, name)
                    if info is not None and stat.S_ISREG(info.st_mode):
                        os.unlink(name, dir_fd=leaf)
            clean = (
                prune(destination, spec.folder, folders_of(f.target for f in spec.files)) and clean
            )
        except FileNotFoundError:
            pass
        remove_tree(staging, archive_name(spec))
        record(staging, spec, receipt, "removed")
    return clean


# Database and Audiobookshelf orchestration.


SEPARATION_STAGES = {"restoring", "restored", "separated"}


def connect(connection):
    from app.domain.libro_library import library_token

    base_url, secrets = connection
    return Audiobookshelf(base_url, library_token(secrets))


async def part_sets(db, *, integration_id=None, library_id=None, version_id=None, version_ids=None):
    """Audiobookshelf part items of each recording, grouped by library and version."""
    query = (
        select(
            LibraryAsset.id,
            LibraryAsset.library_id,
            LibraryAsset.version_id,
            LibraryAsset.external_id,
            LibraryAsset.metadata_snapshot,
            AssetContains.work_id,
            AssetContains.part_index,
            AssetContains.part_total,
        )
        .join(AssetContains, AssetContains.asset_id == LibraryAsset.id)
        .join(Library, Library.id == LibraryAsset.library_id)
        .join(Integration, Integration.id == Library.integration_id)
        .where(
            AssetContains.part_index.is_not(None),
            AssetContains.verified.is_(True),
            LibraryAsset.full_content.is_(True),
            LibraryAsset.state.in_(["present", "stale"]),
            LibraryAsset.medium == "audio",
            LibraryAsset.version_id.is_not(None),
            Integration.kind == "audiobookshelf",
            Integration.enabled.is_(True),
            Library.accessible.is_(True),
        )
        .order_by(LibraryAsset.library_id, LibraryAsset.version_id, AssetContains.part_index)
    )
    if integration_id:
        query = query.where(Library.integration_id == integration_id)
    if library_id:
        query = query.where(LibraryAsset.library_id == library_id)
    if version_id:
        query = query.where(LibraryAsset.version_id == version_id)
    if version_ids is not None:
        query = query.where(LibraryAsset.version_id.in_(version_ids))
    groups = {}
    for row in await db.execute(query):
        groups.setdefault((row.library_id, row.version_id), []).append(row)
    return groups


def incomplete(rows):
    """Why a part set isn't ready to combine, or None when every part is there once."""
    total = rows[0].part_total
    if any(row.part_total != total for row in rows):
        return "The parts disagree on how many parts the book has"
    counts = Counter(row.part_index for row in rows)
    missing = [str(index) for index in range(1, total + 1) if index not in counts]
    if missing:
        label = "part" if len(missing) == 1 else "parts"
        return f"Waiting for {label} {', '.join(missing)} of {total}"
    doubled = sorted(index for index, count in counts.items() if count > 1)
    if doubled:
        return f"Two library items both hold part {doubled[0]}"
    return None


async def statuses(db, version_ids):
    """Combine status of every part set of these recordings, keyed by (library, version)."""
    from app.domain.catalog_metadata import preferences

    automatic = (await preferences(db)).combine_library_parts
    sets = await part_sets(db, version_ids=version_ids)
    rows = {
        (row.library_id, row.version_id): row
        for row in await db.scalars(
            select(PartCombine).where(PartCombine.version_id.in_(version_ids))
        )
    }
    result = {}
    for key in {*sets, *rows}:
        members, row = sets.get(key, []), rows.get(key)
        stage = (row.plan or {}).get("stage") if row else None
        spec = (row.plan or {}).get("spec") if row else None
        waiting = incomplete(members) if members else None
        if spec:
            total = len(spec["parts"])
            present = [part["index"] for part in spec["parts"]]
        elif members:
            total = members[0].part_total
            present = sorted({member.part_index for member in members})
        else:
            continue
        state, reason = (row.state, row.reason) if row else ("ready", None)
        if state in {"skipped", "ready"} and waiting:
            state, reason = "waiting", waiting
        elif state == "ready":
            reason = (
                "Dewarr combines these into one book after the next library sync"
                if automatic
                else "Automatic combining is off; combine them when you're ready"
            )
        separating = stage in SEPARATION_STAGES or state == "separating"
        result[key] = {
            "library_id": key[0],
            "version_id": key[1],
            "total": total,
            "present": present,
            "state": state,
            "reason": reason,
            "folder": spec["folder"] if spec else None,
            "can_combine": bool(members)
            and not waiting
            and not separating
            and state in {"ready", "skipped", "separated", "needs-attention"},
            "can_separate": (state == "combined" and stage == "done")
            or (state == "needs-attention" and separating),
        }
    return result


async def evaluate(db, library_id, version_id, rows):
    """Everything needed to plan the combine, or CombineSkipped with the reason."""
    from app.importing.destination_view import view as destination_view
    from app.importing.destinations import destination_configuration
    from app.importing.metadata import ExportMetadata, initial_sidecars
    from app.importing.naming import NamingMetadata, media_folder, render, values_for
    from app.importing.planning import filing_series
    from app.importing.settings import current_profile
    from app.importing.storage import shared_library_roots

    if reason := incomplete(rows):
        raise CombineSkipped(reason)
    version = await db.get(Version, version_id)
    work = await db.get(Work, version.work_id) if version else None
    if not version or not work:
        raise CombineSkipped("This recording is no longer in the catalog")
    whole = await db.scalar(
        select(LibraryAsset.id)
        .join(AssetContains, AssetContains.asset_id == LibraryAsset.id)
        .where(
            LibraryAsset.library_id == library_id,
            LibraryAsset.version_id == version_id,
            LibraryAsset.state.in_(["present", "stale"]),
            LibraryAsset.full_content.is_(True),
            AssetContains.part_index.is_(None),
            AssetContains.verified.is_(True),
        )
        .limit(1)
    )
    if whole:
        raise CombineSkipped("The whole book is already in this library as one item")
    importing = await db.scalar(
        select(ImportEntry.id)
        .where(ImportEntry.version_id == version_id, ImportEntry.state.in_(ACTIVE_IMPORTS))
        .limit(1)
    )
    if importing:
        raise CombineSkipped("An import of this recording is still running")
    paths = {}
    for row in rows:
        path = (row.metadata_snapshot or {}).get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise CombineSkipped(
                f"Audiobookshelf hasn't reported the folder for part {row.part_index}; "
                "sync the library again"
            )
        paths[row.part_index] = PurePosixPath(path)
    destinations = (
        await db.scalars(
            select(ImportDestination)
            .where(
                ImportDestination.library_id == library_id,
                ImportDestination.medium == "audio",
                ImportDestination.enabled.is_(True),
            )
            .order_by(ImportDestination.root_key)
        )
    ).all()
    if not destinations:
        raise CombineSkipped(
            "Set this library up as an audio import destination so Dewarr can reorganize "
            "its folders"
        )
    chosen = outside = None
    for row in destinations:
        current = await destination_view(db, row)
        configuration = await destination_configuration(db, row)
        if (
            not current.probe
            or current.probe.get("status") != "verified"
            or not current.probe.get("backend", {}).get("root_mapping")
            or not configuration["root_path"]
            or not configuration["staging_path"]
        ):
            continue
        base = PurePosixPath(row.backend_path)
        stray = next(
            (index for index, path in sorted(paths.items()) if not path.is_relative_to(base)),
            None,
        )
        if stray is None and all(path != base for path in paths.values()):
            chosen = (row, configuration)
            break
        outside = (stray, row.backend_path)
    if not chosen:
        if outside and outside[0]:
            raise CombineSkipped(
                f"Part {outside[0]} is outside the mapped library folder {outside[1]}"
            )
        raise CombineSkipped(
            "Verify this library's audio import destination so Dewarr can reorganize its folders"
        )
    destination, configuration = chosen
    profile = await current_profile(db)
    series, sequence = await filing_series(db, work)
    title = parse_title_labels(version.title or work.title).title or work.title
    asin = version.identifiers.get("asin")
    try:
        metadata = NamingMetadata(
            title=title,
            authors=work.authors,
            series=series,
            sequence=sequence,
            narrators=version.narrators,
            language=version.language or work.language,
            abridged=version.abridged,
            original_year=work.publication_year,
            recording_year=version.publication_year,
            asin=asin if isinstance(asin, str) else None,
        )
        folder = media_folder(
            render(profile.audio_folder, values_for(metadata, medium="audio")),
            "audio",
            shared_library=Path(configuration["root_path"]) in await shared_library_roots(db),
        )
        sidecars = initial_sidecars(
            ExportMetadata(medium="audio", naming=metadata, description=work.description)
        )
    except ValueError as error:
        raise CombineSkipped(f"The book folder can't be named: {error}") from error
    base = PurePosixPath(destination.backend_path)
    return {
        "destination_id": destination.id,
        "context": {
            "root": Path(configuration["root_path"]),
            "staging": Path(configuration["staging_path"]),
            "journals": Path(configuration["journal_path"])
            if configuration.get("journal_path")
            else None,
            "backend_path": destination.backend_path,
            "folder": folder,
            "sidecars": sidecars,
            "parts": [
                {
                    "index": row.part_index,
                    "total": row.part_total,
                    "asset_id": row.id,
                    "item_id": row.external_id,
                    "source": str(paths[row.part_index].relative_to(base)),
                    "audio": {
                        file["path"]: file["size"]
                        for file in (row.metadata_snapshot or {}).get("audio") or []
                    },
                }
                for row in rows
            ],
        },
    }


async def find_items(client, library_external_id, paths):
    """Present, readable items at exactly these backend paths."""
    found, page = {}, 0
    while True:
        rows, total = await client.page(library_external_id, page)
        ids = [row["id"] for row in rows if row.get("path") in paths or "path" not in row]
        for item in await client.expanded(ids) if ids else []:
            if item.path in paths and not item.missing and not item.unreadable:
                if item.path in found:
                    raise CombineError("Audiobookshelf reports two items for one folder")
                found[item.path] = item
        if (page + 1) * client.page_size >= total:
            return found
        page += 1


def audio_of(item):
    return {file.path: file.size for file in item.audio}


async def await_items(client, library_external_id, expected):
    """Scan, then wait until each backend path holds exactly its expected audio files."""
    await client.scan(library_external_id)
    for attempt in range(CONFIRM_ATTEMPTS):
        try:
            found = await find_items(client, library_external_id, set(expected))
        except AdapterError as error:
            if error.kind != FailureKind.UNCERTAIN:
                raise
            found = {}
        if all(
            path in found and audio_of(found[path]) == audio for path, audio in expected.items()
        ):
            return found
        if attempt + 1 < CONFIRM_ATTEMPTS:
            await asyncio.sleep(CONFIRM_DELAY)
    return None


async def remove_old_items(client, library_external_id, item_ids):
    """Drop the old part entries Audiobookshelf now reports as missing."""
    if not item_ids:
        return []
    await client.scan(library_external_id)
    pending = list(item_ids)
    for attempt in range(CONFIRM_ATTEMPTS):
        pending = [item for item in pending if not await client.remove_missing_item(item)]
        if not pending:
            return []
        if attempt + 1 < CONFIRM_ATTEMPTS:
            await asyncio.sleep(CONFIRM_DELAY)
    return pending


async def link_item(db, integration, library, version, item, part=None):
    """Record a confirmed item as this recording, the whole book or one part of it."""
    from app.domain.inventory import apply_item

    namespace = f"abs:{integration.id}"
    link = await db.scalar(
        select(ProviderObject)
        .where(
            ProviderObject.provider == namespace,
            ProviderObject.kind == "item:audio",
            ProviderObject.external_id == item.id,
        )
        .with_for_update()
    )
    if link and link.manual_lock and link.version_id not in {None, version.id}:
        raise CombineError("A manual library match conflicts with this recording")
    if not link:
        link = ProviderObject(provider=namespace, kind="item:audio", external_id=item.id)
        db.add(link)
    link.work_id, link.version_id, link.manual_lock, link.match_status = (
        version.work_id,
        version.id,
        True,
        "matched",
    )
    link.snapshot = item.model_dump(mode="json")
    previous = await db.scalar(
        select(LibraryAsset).where(
            LibraryAsset.library_id == library.id,
            LibraryAsset.external_id == item.id,
            LibraryAsset.medium == "audio",
        )
    )
    if previous and previous.state == "intentionally-removed":
        previous.state = "present"
    await db.flush()
    await apply_item(db, library, item, library.generation, integration.id, {item.id})
    await db.flush()
    asset = await db.scalar(
        select(LibraryAsset).where(
            LibraryAsset.library_id == library.id,
            LibraryAsset.external_id == item.id,
            LibraryAsset.medium == "audio",
        )
    )
    if not asset or not asset.full_content or asset.version_id != version.id:
        raise CombineError("Audiobookshelf's item did not record as a complete copy of this book")
    coverage = await db.get(AssetContains, (asset.id, version.work_id))
    coverage.verified = True
    coverage.part_index, coverage.part_total = part or (None, None)
    return asset


async def row_for(db, library_id, version_id, work_id=None):
    row = await db.scalar(
        select(PartCombine)
        .where(PartCombine.library_id == library_id, PartCombine.version_id == version_id)
        .with_for_update()
    )
    if not row and work_id:
        row = PartCombine(
            library_id=library_id,
            version_id=version_id,
            work_id=work_id,
            state="skipped",
            part_asset_ids=[],
        )
        db.add(row)
        await db.flush()
    return row


def load_spec(row):
    return CombineSpec.model_validate(row.plan["spec"])


async def settle(library_id, version_id, state, reason=None, **plan):
    async with session_factory()() as db, db.begin():
        row = await row_for(db, library_id, version_id)
        row.state, row.reason = state, reason
        if plan:
            row.plan = {**(row.plan or {}), **plan}
    return state


async def combine_one(owner_id, library_id, version_id, operation_id=None):
    """Run or resume one combine. Returns the resulting state."""
    async with session_factory()() as db, db.begin():
        from app.domain.operations import transaction_lock

        await transaction_lock(db, f"combine:{library_id}:{version_id}")
        rows = (await part_sets(db, library_id=library_id, version_id=version_id)).get(
            (library_id, version_id), []
        )
        existing = await row_for(db, library_id, version_id)
        resuming = bool(
            existing
            and existing.state in {"combining", "needs-attention"}
            and existing.plan
            and existing.plan.get("stage") not in {None, "planned", *SEPARATION_STAGES}
        )
        if not resuming and not rows:
            return existing.state if existing else None
        library = await db.get(Library, library_id)
        integration = await db.get(Integration, library.integration_id)
        connection = (integration.base_url, integration.encrypted_secrets)
        external = library.external_id
        planned = None
        if not resuming:
            row = await row_for(db, library_id, version_id, rows[0].work_id)
            try:
                planned = await evaluate(db, library_id, version_id, rows)
            except CombineSkipped as skipped:
                row.state, row.reason, row.plan = "skipped", str(skipped), None
                return "skipped"
            row.operation_id = operation_id
    if planned:
        async with connect(connection) as client:
            try:
                started = await client.started_items()
            except AdapterError as error:
                return await settle(
                    library_id,
                    version_id,
                    "skipped",
                    f"Couldn't read listening progress from Audiobookshelf: {error}",
                )
        heard = sorted(
            part["index"] for part in planned["context"]["parts"] if part["item_id"] in started
        )
        if heard:
            return await settle(
                library_id,
                version_id,
                "skipped",
                f"Your Audiobookshelf account has started part {heard[0]}; combining would "
                "reset that listening progress",
            )
        try:
            spec = await asyncio.to_thread(
                build_spec, {**planned["context"], "combine_id": uuid4()}
            )
        except CombineSkipped as skipped:
            return await settle(library_id, version_id, "skipped", str(skipped))
        async with session_factory()() as db, db.begin():
            row = await row_for(db, library_id, version_id)
            row.state, row.reason = "combining", None
            row.destination_id = planned["destination_id"]
            row.part_asset_ids = [str(part.asset_id) for part in spec.parts]
            row.combined_asset_id = None
            row.plan = {"stage": "planned", "spec": spec.model_dump(mode="json")}
    async with session_factory()() as db:
        row = await row_for(db, library_id, version_id)
        spec, stage = load_spec(row), row.plan.get("stage")
        await db.rollback()
    if stage == "planned":
        try:
            await asyncio.to_thread(publish_combined, spec)
        except (CombineSkipped, CombineError, PublicationError, OSError) as error:
            try:
                await asyncio.to_thread(discard, spec)
            except (CombineError, PublicationError, OSError):
                return await settle(
                    library_id,
                    version_id,
                    "needs-attention",
                    f"The combined folder was published but couldn't be verified: {error}",
                    stage="published",
                )
            return await settle(library_id, version_id, "skipped", str(error), stage=None)
        await settle(library_id, version_id, "combining", stage="published")
        stage = "published"
    async with connect(connection) as client:
        if stage == "published":
            found = await await_items(
                client, external, {spec.backend(spec.folder): spec.expected_audio()}
            )
            if not found:
                return await settle(
                    library_id,
                    version_id,
                    "needs-attention",
                    "Waiting for Audiobookshelf to show the combined book; Dewarr checks again "
                    "after the next sync",
                )
            item = found[spec.backend(spec.folder)]
            async with session_factory()() as db, db.begin():
                row = await row_for(db, library_id, version_id)
                library = await db.get(Library, library_id)
                integration = await db.get(Integration, library.integration_id)
                version = await db.get(Version, version_id)
                try:
                    asset = await link_item(db, integration, library, version, item)
                except CombineError as error:
                    await db.rollback()
                    return await settle(library_id, version_id, "needs-attention", str(error))
                old = [part for part in spec.parts if part.asset_id != asset.id]
                for part in old:
                    retired = await db.get(LibraryAsset, part.asset_id)
                    if retired:
                        retired.state, retired.missing_since = "intentionally-removed", None
                row.combined_asset_id = asset.id
                row.plan = {**row.plan, "stage": "handed-over", "item_id": item.id}
                db.add(
                    AuditEvent(
                        actor_id=owner_id,
                        action="library.parts.combined",
                        entity_id=asset.id,
                        detail={
                            "version_id": str(version_id),
                            "folder": spec.folder,
                            "item_id": item.id,
                            "part_assets": [str(part.asset_id) for part in spec.parts],
                        },
                    )
                )
            await asyncio.to_thread(mark, spec, "handed-over")
            stage = "handed-over"
        async with session_factory()() as db:
            row = await row_for(db, library_id, version_id)
            new_item = row.plan.get("item_id")
            await db.rollback()
        if stage == "handed-over":
            if await asyncio.to_thread(journal_state, spec) == "published":
                await asyncio.to_thread(mark, spec, "handed-over")
            left = await asyncio.to_thread(cleanup_parts, spec)
            await settle(library_id, version_id, "combining", stage="cleaned", left=left)
            stage = "cleaned"
        if stage == "cleaned":
            async with session_factory()() as db:
                row = await row_for(db, library_id, version_id)
                left = row.plan.get("left") or []
                await db.rollback()
            old_items = [
                part.item_id
                for part in spec.parts
                if part.item_id != new_item and part.source not in left
            ]
            lingering = await remove_old_items(client, external, old_items)
            await asyncio.to_thread(mark, spec, "done")
            notes = []
            if left:
                notes.append(
                    "Left "
                    + ", ".join(left)
                    + " in place because it held files added after the plan"
                )
            if lingering:
                notes.append("Audiobookshelf still lists an old part entry; run its scan again")
            return await settle(
                library_id,
                version_id,
                "combined",
                "; ".join(notes) or None,
                stage="done",
            )
    return stage


async def separate_one(owner_id, library_id, version_id):
    """Undo a finished combine: the parts return to their own folders and items."""
    async with session_factory()() as db, db.begin():
        from app.domain.operations import transaction_lock

        await transaction_lock(db, f"combine:{library_id}:{version_id}")
        row = await row_for(db, library_id, version_id)
        if (
            not row
            or not row.plan
            or row.plan.get("stage")
            not in {
                "done",
                "restoring",
                "restored",
                "separated",
            }
        ):
            return row.state if row else None
        spec, stage = load_spec(row), row.plan["stage"]
        combined_item = row.plan.get("item_id")
        library = await db.get(Library, library_id)
        integration = await db.get(Integration, library.integration_id)
        connection = (integration.base_url, integration.encrypted_secrets)
        external = library.external_id
        row.state, row.reason = "separating", None
    async with connect(connection) as client:
        if stage == "done":
            try:
                started = await client.started_items()
            except AdapterError as error:
                return await settle(
                    library_id,
                    version_id,
                    "combined",
                    f"Couldn't read listening progress from Audiobookshelf: {error}",
                )
            if combined_item in started:
                return await settle(
                    library_id,
                    version_id,
                    "combined",
                    "Your Audiobookshelf account has started the combined book; separating "
                    "would reset that listening progress",
                )
            try:
                await asyncio.to_thread(restore_parts, spec)
            except (CombineError, PublicationError, OSError) as error:
                return await settle(
                    library_id,
                    version_id,
                    "needs-attention",
                    f"Couldn't put the parts back: {error}",
                    stage="restoring",
                )
            await settle(library_id, version_id, "separating", stage="restored")
            stage = "restored"
        if stage == "restoring":
            await asyncio.to_thread(restore_parts, spec)
            stage = "restored"
        if stage == "restored":
            expected = {spec.backend(part.source): part.audio for part in spec.parts}
            found = await await_items(client, external, expected)
            if not found:
                return await settle(
                    library_id,
                    version_id,
                    "needs-attention",
                    "Waiting for Audiobookshelf to show the separated parts",
                    stage="restored",
                )
            async with session_factory()() as db, db.begin():
                row = await row_for(db, library_id, version_id)
                library = await db.get(Library, library_id)
                integration = await db.get(Integration, library.integration_id)
                version = await db.get(Version, version_id)
                assets = []
                try:
                    for part in spec.parts:
                        assets.append(
                            await link_item(
                                db,
                                integration,
                                library,
                                version,
                                found[spec.backend(part.source)],
                                (part.index, part.total),
                            )
                        )
                except CombineError as error:
                    await db.rollback()
                    return await settle(library_id, version_id, "needs-attention", str(error))
                kept = {asset.id for asset in assets}
                if row.combined_asset_id and row.combined_asset_id not in kept:
                    combined = await db.get(LibraryAsset, row.combined_asset_id)
                    if combined:
                        combined.state, combined.missing_since = "intentionally-removed", None
                db.add(
                    AuditEvent(
                        actor_id=owner_id,
                        action="library.parts.separated",
                        entity_id=row.combined_asset_id,
                        detail={
                            "version_id": str(version_id),
                            "folder": spec.folder,
                            "part_assets": [str(asset.id) for asset in assets],
                        },
                    )
                )
                row.part_asset_ids = [str(asset.id) for asset in assets]
                row.plan = {
                    **row.plan,
                    "stage": "separated",
                    "part_items": [found[spec.backend(part.source)].id for part in spec.parts],
                }
            await asyncio.to_thread(mark, spec, "separated")
            stage = "separated"
        if stage == "separated":
            clean = await asyncio.to_thread(remove_combined, spec)
            async with session_factory()() as db:
                row = await row_for(db, library_id, version_id)
                part_items = set(row.plan.get("part_items") or [])
                await db.rollback()
            lingering = (
                await remove_old_items(client, external, [combined_item])
                if combined_item and combined_item not in part_items
                else []
            )
            notes = []
            if not clean:
                notes.append(f"Left {spec.folder} in place because it held other files")
            if lingering:
                notes.append("Audiobookshelf still lists the combined entry; run its scan again")
            async with session_factory()() as db, db.begin():
                row = await row_for(db, library_id, version_id)
                row.state, row.reason = "separated", "; ".join(notes) or None
                row.combined_asset_id = None
                row.plan = None
            return "separated"
    return stage


async def schedule_library_combine(db, owner_id, integration_id, run_id):
    """Queue a combine pass after a sync when a part set is complete or a combine is pending."""
    from app.domain.catalog_metadata import preferences
    from app.jobs.queue import enqueue

    if get_settings().recovery_mode or not (await preferences(db)).combine_library_parts:
        return None
    sets = await part_sets(db, integration_id=integration_id)
    ready = [key for key, rows in sets.items() if not incomplete(rows)]
    pending = await db.scalar(
        select(PartCombine.id)
        .join(Library, Library.id == PartCombine.library_id)
        .where(
            Library.integration_id == integration_id,
            PartCombine.state.in_(["combining", "needs-attention", "separating"]),
        )
        .limit(1)
    )
    if not ready and not pending:
        return None
    running = await db.scalar(
        select(Operation.id).where(
            Operation.kind == "library.combine",
            Operation.integration_id == integration_id,
            Operation.status.not_in(TERMINAL),
        )
    )
    if running:
        return None
    operation = Operation(
        owner_id=owner_id,
        kind="library.combine",
        idempotency_key=f"library-combine:{run_id}",
        integration_id=integration_id,
        payload={"run_id": str(run_id), "automatic": True},
        message="Waiting to combine multi-part books",
    )
    db.add(operation)
    await db.flush()
    operation.job_id = await enqueue(db, "library.combine", operation_id=str(operation.id))
    return operation


async def request_combine(db, owner_id, library_id, version_id, action):
    """Queue a manual combine or separate for one book."""
    from app.jobs.queue import enqueue

    library = await db.get(Library, library_id)
    operation = Operation(
        owner_id=owner_id,
        kind="library.combine",
        idempotency_key=f"library-{action}:{library_id}:{version_id}:{uuid4()}",
        integration_id=library.integration_id if library else None,
        payload={
            "library_id": str(library_id),
            "version_id": str(version_id),
            "action": action,
        },
        message="Waiting to combine the parts"
        if action == "combine"
        else "Waiting to separate the parts",
    )
    db.add(operation)
    await db.flush()
    operation.job_id = await enqueue(db, "library.combine", operation_id=str(operation.id))
    return operation


async def run(operation_id):
    """The library.combine job: one requested book, or every ready book after a sync."""
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id, with_for_update=True)
        if not operation or operation.kind != "library.combine" or operation.status in TERMINAL:
            return
        user = await db.get(User, operation.owner_id)
        payload = dict(operation.payload)
        if get_settings().recovery_mode or not user or not user.active or user.role != "admin":
            operation.status, operation.message = "cancelled", "Combining parts is paused"
            return
        owner_id, integration_id = operation.owner_id, operation.integration_id
        if payload.get("automatic"):
            from app.domain.catalog_metadata import preferences

            if not (await preferences(db)).combine_library_parts:
                operation.status, operation.message = (
                    "cancelled",
                    "Combining multi-part books was turned off",
                )
                return
            sets = await part_sets(db, integration_id=integration_id)
            rows = {
                (row.library_id, row.version_id): (
                    row.state,
                    "separate"
                    if row.state == "separating"
                    or (row.plan or {}).get("stage") in SEPARATION_STAGES
                    else "combine",
                )
                for row in await db.scalars(
                    select(PartCombine)
                    .join(Library, Library.id == PartCombine.library_id)
                    .where(Library.integration_id == integration_id)
                )
            }
            # A book the admin separated stays separated until they combine it again.
            keys = [
                key
                for key, members in sets.items()
                if not incomplete(members)
                and rows.get(key, (None, "combine"))[1] == "combine"
                and rows.get(key, (None,))[0] not in {"separated", "combined"}
            ] + [
                key
                for key, (state, action) in rows.items()
                if state in {"combining", "needs-attention"}
                and action == "combine"
                and key not in sets
            ]
            separating = [
                key
                for key, (state, action) in rows.items()
                if action == "separate" and state in {"separating", "needs-attention"}
            ]
        else:
            key = (UUID(payload["library_id"]), UUID(payload["version_id"]))
            keys, separating = ([key], []) if payload["action"] == "combine" else ([], [key])
        operation.status, operation.message = "running", "Combining multi-part books"
    results = []
    failure = None
    for library_id, version_id in keys:
        try:
            results.append(await combine_one(owner_id, library_id, version_id, operation_id))
        except (AdapterError, PublicationError, OSError) as error:
            failure = str(error)
            await settle(library_id, version_id, "needs-attention", failure)
    for library_id, version_id in separating:
        try:
            results.append(await separate_one(owner_id, library_id, version_id))
        except (AdapterError, PublicationError, OSError) as error:
            failure = str(error)
            await settle(library_id, version_id, "needs-attention", failure)
    async with session_factory()() as db, db.begin():
        operation = await db.get(Operation, operation_id, with_for_update=True)
        combined = results.count("combined")
        separated = results.count("separated")
        operation.status = "completed"
        if payload.get("automatic"):
            operation.message = f"Combined {combined} multi-part books" + (
                f"; {len(keys) - combined} need attention or were skipped"
                if len(keys) > combined
                else ""
            )
        elif payload["action"] == "combine":
            operation.message = (
                "Combined the parts into one book" if combined else "The parts were not combined"
            )
        else:
            operation.message = (
                "Separated the parts again" if separated else "The parts were not separated"
            )
        if failure:
            operation.message += f"; {failure}"
        operation.payload = {**payload, "results": results}
