"""Journaled, no-replace publication of one complete item from a frozen manifest.

All recovery bookkeeping stays in the private staging root, never in an ABS library.
"""

import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.importing.converters import AudioConversion, convert, descriptor_path
from app.importing.filesystem import (
    InspectionError,
    beneath,
    digest,
    directory,
    enumerate_files,
    identity,
    relative_parts,
    source_scope,
)
from app.importing.naming import StrictModel, collision_key, fingerprint


class PublicationError(InspectionError):
    pass


class PublicationBusy(PublicationError):
    pass


PUBLICATION_MARKER = ".book-search-publication"


class PublishFile(StrictModel):
    source: str
    name: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity: dict[str, int]

    @model_validator(mode="after")
    def confined(self):
        relative_parts(self.source)
        if len(relative_parts(self.name)) != 1:
            raise ValueError("Published media filenames must be within the item leaf")
        return self


class PublicationSpec(StrictModel):
    entry_id: UUID
    plan_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_root: Path
    source_relative: str
    source_kind: Literal["directory", "file"] = "directory"
    source_directory: dict[str, int]
    destination_root: Path
    staging_root: Path
    folder: str
    mode: Literal["hardlink", "copy", "rename"] = "hardlink"
    files: list[PublishFile] = Field(default_factory=list, max_length=5000)
    conversion: AudioConversion | None = None
    sidecars: dict[str, str] = Field(default_factory=dict)
    binary_sidecars: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def confined(self):
        relative_parts(self.source_relative)
        if self.conversion and self.source_kind != "directory":
            raise ValueError("Chapter merging requires the download directory")
        if not self.files and self.conversion is None:
            raise ValueError("Publish at least one media file")
        if self.source_kind == "file" and (
            len(self.files) != 1 or self.files[0].source != PurePosixPath(self.source_relative).name
        ):
            raise ValueError("A single-file import can publish only its inspected file")
        relative_parts(self.folder)
        names = [file.name for file in self.files]
        if self.conversion:
            names.append(self.conversion.output_name)
        for name in self.sidecars:
            if name not in {"metadata.opf", "reader.txt", "desc.txt"} and not re.fullmatch(
                r"[^/\\.\x00][^/\\\x00]{0,180}\.metadata\.json", name
            ):
                raise ValueError(
                    "Only independently generated supported metadata sidecars are allowed"
                )
        if sum(len(value.encode()) for value in self.sidecars.values()) > 1024 * 1024:
            raise ValueError("Generated sidecars exceed the supported limit")
        names.extend(self.sidecars)
        if set(self.binary_sidecars) - {"cover.jpg"}:
            raise ValueError("Only a generated JPEG cover may be exported as binary metadata")
        for content in self.binary_sidecars.values():
            if len(content) > 700000:
                raise ValueError("Generated cover exceeds its size limit")
            decoded = base64.b64decode(content, validate=True)
            if (
                len(decoded) > 512 * 1024
                or not decoded.startswith(b"\xff\xd8")
                or not decoded.endswith(b"\xff\xd9")
            ):
                raise ValueError("Generated cover is not a bounded JPEG")
        names.extend(self.binary_sidecars)
        if PUBLICATION_MARKER in names:
            raise ValueError("The publication marker name is reserved")
        if len({collision_key(name) for name in names}) != len(names):
            raise ValueError("Published filenames collide")
        for root in (self.source_root, self.destination_root, self.staging_root):
            if not root.is_absolute() or str(root) == "/" or ".." in root.parts:
                raise ValueError("Use absolute non-root paths")
        for left, right in (
            (self.source_root, self.destination_root),
            (self.source_root, self.staging_root),
            (self.destination_root, self.staging_root),
        ):
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise ValueError("Source, library and staging roots must not overlap")
        return self


def generated_files(spec):
    return {
        **{name: content.encode() for name, content in spec.sidecars.items()},
        **{
            name: base64.b64decode(content, validate=True)
            for name, content in spec.binary_sidecars.items()
        },
    }


def specification_fingerprint(spec):
    payload = spec.model_dump(mode="json")
    if spec.source_kind == "directory":
        payload.pop("source_kind")  # Preserve existing directory publication receipts.
    if not spec.binary_sidecars:
        payload.pop("binary_sidecars")  # Preserve receipts created before cover support.
    if spec.conversion is None:
        payload.pop("conversion")  # Preserve receipts created before chapter merging.
    return fingerprint(payload)


def object_id(fd):
    value = os.fstat(fd)
    return {"device": value.st_dev, "inode": value.st_ino}


def same_object(fd, expected):
    return object_id(fd) == {key: expected[key] for key in ("device", "inode")}


def publication_marker_content(receipt):
    token = receipt.get("publication_marker")
    if not token:
        return None
    return f"book-search publication {receipt['entry_id']} {token}\n".encode()


def has_publication_marker(folder, receipt):
    content = publication_marker_content(receipt)
    if content is None:
        return False
    try:
        with beneath(folder, PUBLICATION_MARKER) as marker:
            info = os.fstat(marker)
            if info.st_size != len(content) or info.st_nlink != 1:
                return False
            os.lseek(marker, 0, os.SEEK_SET)
            return os.read(marker, len(content) + 1) == content
    except (FileNotFoundError, InspectionError):
        return False


def ensure_publication_marker(stage, staging, receipt_name, receipt):
    if not receipt.get("publication_marker"):
        receipt["publication_marker"] = uuid4().hex
        write_receipt(staging, receipt_name, receipt)
    content = publication_marker_content(receipt)
    try:
        marker = os.open(
            PUBLICATION_MARKER,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=stage,
        )
    except FileExistsError:
        if not has_publication_marker(stage, receipt):
            raise PublicationError("Staged publication marker changed") from None
        return
    try:
        write_all(marker, content)
        os.fsync(marker)
    finally:
        os.close(marker)
    sync_directory(stage)


def remove_publication_marker(folder, receipt):
    try:
        if not has_publication_marker(folder, receipt):
            return False
        os.unlink(PUBLICATION_MARKER, dir_fd=folder)
        sync_directory(folder)
        return True
    except FileNotFoundError:
        return False


def seeding_same_file(fd, expected):
    """A same-device rename keeps the inode. A cross-filesystem move only keeps the bytes."""
    info = os.fstat(fd)
    if info.st_dev != expected["device"]:
        return True
    return info.st_ino == expected["inode"]


# NFS, SMB, 9p and some FUSE filesystems reject the no-replace rename flag with these.
NO_REPLACE_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}
HARDLINKS_UNSUPPORTED = {errno.EPERM, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}


def native_no_replace(source_fd, source_name, destination_fd, destination_name):
    if sys.platform not in {"darwin", "linux"}:
        raise PublicationError("This platform has no supported no-replace publication operation")
    # Interface constants from Linux renameat2 and Darwin renameatx_np.
    libc = ctypes.CDLL(None, use_errno=True)
    symbol, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    function = getattr(libc, symbol, None)
    if function is None:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        source_fd, os.fsencode(source_name), destination_fd, os.fsencode(destination_name), flag
    )
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def no_replace(source_fd, source_name, destination_fd, destination_name):
    """Rename without replacing an existing name. Returns "native" or "fallback"."""
    try:
        native_no_replace(source_fd, source_name, destination_fd, destination_name)
        return "native"
    except OSError as error:
        if error.errno not in NO_REPLACE_UNSUPPORTED:
            raise
    info = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        _rename_directory(source_fd, source_name, destination_fd, destination_name, info)
    elif stat.S_ISREG(info.st_mode):
        _rename_file(source_fd, source_name, destination_fd, destination_name, info)
    else:
        raise PublicationError("Only files and directories can be published")
    return "fallback"


def _arrived(fd, name, info):
    # NFS can replay a completed link or rename and report EEXIST or ENOENT for our own object.
    try:
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)


def _exists(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _rename_directory(source_fd, source_name, destination_fd, destination_name, info):
    # rename(2) never replaces a file or a non-empty directory, so the name check only
    # leaves a race in which another process's empty directory could be replaced.
    if _exists(destination_fd, destination_name):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination_name)
    try:
        os.rename(source_name, destination_name, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
    except FileNotFoundError:
        if not _arrived(destination_fd, destination_name, info):
            raise


def _rename_file(source_fd, source_name, destination_fd, destination_name, info):
    try:
        os.link(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        if not _arrived(destination_fd, destination_name, info):
            raise
    except OSError as error:
        if error.errno not in HARDLINKS_UNSUPPORTED:
            raise
        _claim_and_rename(source_fd, source_name, destination_fd, destination_name)
        return
    os.unlink(source_name, dir_fd=source_fd)


def _claim_and_rename(source_fd, source_name, destination_fd, destination_name):
    # O_EXCL reserves the name; the rename then replaces only our own empty claim.
    # An interrupted claim stays behind as an empty file (see read_receipt).
    claim = os.open(
        destination_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=destination_fd,
    )
    try:
        owned = object_id(claim)
    finally:
        os.close(claim)
    try:
        current = os.stat(destination_name, dir_fd=destination_fd, follow_symlinks=False)
        if {"device": current.st_dev, "inode": current.st_ino} != owned or current.st_size:
            raise PublicationError("No-replace claim changed before rename")
        os.rename(source_name, destination_name, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
    except BaseException:
        try:
            current = os.stat(destination_name, dir_fd=destination_fd, follow_symlinks=False)
            if {"device": current.st_dev, "inode": current.st_ino} == owned and not current.st_size:
                os.unlink(destination_name, dir_fd=destination_fd)
        except FileNotFoundError:
            pass
        raise


def sync_directory(fd):
    try:
        os.fsync(fd)
    except OSError as error:
        # Some SMB clients cannot fsync a directory; the server applies namespace changes
        # before replying, so there is nothing left to flush.
        if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise


@contextmanager
def private_staging(path):
    with directory(path) as fd:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            uid = os.geteuid()
            raise PublicationError(
                f"Staging root must be owned by the worker (uid {uid}) and private (0700). "
                "On SMB/CIFS mounts ownership and permissions come from mount options, "
                f"such as uid={uid},dir_mode=0700"
            )
        yield fd


def _lock_file(staging, name):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=staging)
    except FileExistsError:
        return os.open(name, flags, dir_fd=staging)


def _acquire(fd, message):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise PublicationError("Invalid publication lock file")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise PublicationBusy(message) from error
    except OSError as error:
        if error.errno not in {errno.ENOLCK, *NO_REPLACE_UNSUPPORTED}:
            raise
        raise PublicationError(
            "The staging filesystem does not support file locks. "
            "Mount SMB/CIFS shares with nobrl or use a newer kernel"
        ) from error


@contextmanager
def publication_lock(staging, key):
    """Library-wide lock. pause() drops it while this book is encoding."""
    name = "lock-" + hashlib.sha256(key.encode()).hexdigest()
    fd = _lock_file(staging, name)
    message = "Another worker is publishing to this library"
    try:
        _acquire(fd, message)

        @contextmanager
        def pause():
            fcntl.flock(fd, fcntl.LOCK_UN)
            try:
                yield
            finally:
                pending = sys.exc_info()[0]
                if pending is None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                else:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass

        yield pause
    finally:
        os.close(fd)


@contextmanager
def entry_lock(staging, entry_id):
    name = "lock-entry-" + hashlib.sha256(str(entry_id).encode()).hexdigest()
    fd = _lock_file(staging, name)
    try:
        _acquire(fd, "Another worker is publishing this book")
        yield
    finally:
        os.close(fd)


def write_all(fd, data):
    view = memoryview(data)
    while view:
        size = os.write(fd, view)
        if not size:
            raise PublicationError("Could not write generated import data")
        view = view[size:]


def write_receipt(staging, name, receipt, *, create=False):
    data = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    temporary = f"receipt-{uuid4().hex}.tmp"
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging
    )
    try:
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if create:
            try:
                no_replace(staging, temporary, staging, name)
            except FileExistsError:
                # Callers hold the publication lock, so an empty receipt is an abandoned claim.
                existing = os.stat(name, dir_fd=staging, follow_symlinks=False)
                if not stat.S_ISREG(existing.st_mode) or existing.st_size:
                    raise
                os.replace(temporary, name, src_dir_fd=staging, dst_dir_fd=staging)
        else:
            os.replace(temporary, name, src_dir_fd=staging, dst_dir_fd=staging)
        sync_directory(staging)
    finally:
        try:
            os.unlink(temporary, dir_fd=staging)
        except FileNotFoundError:
            pass


def read_receipt(staging, name):
    try:
        with beneath(staging, name) as fd:
            size = os.fstat(fd).st_size
            if size > 8 * 1024 * 1024:
                raise PublicationError("Publication receipt exceeds its size limit")
            if not size:
                return None  # An interrupted no-replace claim; receipts are never empty.
            with os.fdopen(os.dup(fd), "rb") as stream:
                return json.load(stream)
    except FileNotFoundError:
        return None


def checked_source(source, file, deadline):
    with beneath(source, file.source) as fd:
        before = identity(os.fstat(fd))
        # Creating/removing a hardlink changes ctime legitimately; content and inode remain fixed.
        keys = ("device", "inode", "size", "mtime_ns")
        if any(before[key] != file.identity[key] for key in keys):
            raise PublicationError("Source identity or size changed since inspection")
        if digest(fd, deadline) != file.sha256:
            raise PublicationError("Source bytes changed since inspection")
        after = identity(os.fstat(fd))
        if any(after[key] != before[key] for key in keys):
            raise PublicationError("Source changed while being verified")


def published_names(spec):
    names = {file.name for file in spec.files}
    if spec.conversion:
        names.add(spec.conversion.output_name)
    return names


def conversion_inputs(spec):
    return spec.conversion.chapters if spec.conversion else []


def leaf_names(folder, spec):
    present = set(os.listdir(folder))
    if spec.mode == "rename":
        present.discard(".torrent")
    return present


def verify_item(folder, spec, deadline, derived=None, receipt=None):
    expected = published_names(spec) | set(generated_files(spec))
    present = leaf_names(folder, spec)
    if PUBLICATION_MARKER in present:
        if not receipt or not has_publication_marker(folder, receipt):
            raise PublicationError("Item contains an unrecognized publication marker")
        present.remove(PUBLICATION_MARKER)
    if present != expected:
        raise PublicationError("Item contains missing or unplanned files")
    if spec.conversion:
        recorded = (derived or {}).get(spec.conversion.output_name)
        with beneath(folder, spec.conversion.output_name) as fd:
            if (
                not recorded
                or os.fstat(fd).st_size != recorded["size"]
                or digest(fd, deadline) != recorded["sha256"]
            ):
                raise PublicationError("Converted audiobook does not match its journal")
    for file in spec.files:
        with beneath(folder, file.name) as fd:
            if os.fstat(fd).st_size != file.identity["size"] or digest(fd, deadline) != file.sha256:
                raise PublicationError("Published media does not match its frozen manifest")
            if spec.mode == "hardlink" and not same_object(fd, file.identity):
                raise PublicationError("Published media is not the expected hardlink")
            if spec.mode == "rename" and not seeding_same_file(fd, file.identity):
                raise PublicationError("The library file is not the seeding copy")
    for name, content in generated_files(spec).items():
        with beneath(folder, name) as fd:
            if digest(fd, deadline) != hashlib.sha256(content).hexdigest():
                raise PublicationError("Generated metadata differs from its frozen manifest")


def conflicting_name(fd, name):
    key = collision_key(name)
    return next((existing for existing in os.listdir(fd) if collision_key(existing) == key), None)


@contextmanager
def destination_parent(root, relative):
    parts = relative_parts(relative)
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            existing = conflicting_name(fd, part)
            if existing is not None and existing != part:
                raise PublicationError("Destination folder conflicts with an existing case variant")
            try:
                os.mkdir(part, mode=0o755, dir_fd=fd)
                sync_directory(fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            # Structural ancestors may not contain files that ABS could absorb into a book.
            with os.scandir(fd) as entries:
                if any(not entry.is_dir(follow_symlinks=False) for entry in entries):
                    raise PublicationError("A structural destination folder already contains files")
        yield fd, parts[-1]
    finally:
        os.close(fd)


def prepare_stage(staging, receipt_name, receipt, spec):
    name = receipt["stage_name"]
    if receipt.get("stage_identity"):
        with beneath(staging, name, folder=True) as fd:
            if not same_object(fd, receipt["stage_identity"]):
                raise PublicationError("Staged item identity changed")
            ensure_publication_marker(fd, staging, receipt_name, receipt)
        return
    # A crash between mkdir and identity journaling leaves an unconfirmed, unwatched orphan.
    # Allocate another private staging path; never adopt or remove an unrecognized directory.
    while True:
        try:
            os.mkdir(name, mode=0o755, dir_fd=staging)
            break
        except FileExistsError:
            receipt.setdefault("unconfirmed_stages", []).append(name)
            name = "item-" + uuid4().hex
            receipt["stage_name"] = name
            write_receipt(staging, receipt_name, receipt)
    with beneath(staging, name, folder=True) as fd:
        receipt["stage_identity"] = object_id(fd)
    write_receipt(staging, receipt_name, receipt)
    with beneath(staging, name, folder=True) as fd:
        ensure_publication_marker(fd, staging, receipt_name, receipt)


def stage_conversion(
    staging,
    stage,
    source,
    receipt_name,
    receipt,
    spec,
    deadline,
    checkpoint,
    *,
    should_continue=None,
    on_progress=None,
    pause_library_lock=nullcontext,
):
    plan = spec.conversion
    if plan is None:
        return
    name = plan.output_name
    for chapter in plan.chapters:
        checked_source(source, chapter, deadline)
    checkpoint("before-convert")
    journaled_mismatch = False
    try:
        with beneath(stage, name) as current:
            recorded = receipt.get("derived", {}).get(name)
            info = os.fstat(current)
            matched = (
                recorded
                and info.st_size == recorded["size"]
                and digest(current, deadline) == recorded["sha256"]
            )
            if matched:
                return
            owned = receipt.get("partial_files", {}).get(name)
            if not owned or not same_object(current, owned) or info.st_nlink != 1:
                raise PublicationError("Existing staged audiobook cannot be safely resumed")
            if recorded and info.st_size == recorded["size"]:
                journaled_mismatch = True
        os.unlink(name, dir_fd=stage)
    except FileNotFoundError:
        pass
    if journaled_mismatch:
        _require_free_space(staging, conversion_reservation(spec))
    output = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=stage)
    try:
        receipt.setdefault("partial_files", {})[name] = object_id(output)
        write_receipt(staging, receipt_name, receipt)
        checkpoint("convert-created")
        try:
            with pause_library_lock():
                convert(
                    source,
                    plan,
                    str(Path(descriptor_path(stage)) / name),
                    deadline,
                    should_continue=should_continue,
                    on_progress=on_progress,
                )
                os.fsync(output)
                info = os.fstat(output)
                receipt.setdefault("derived", {})[name] = {
                    "sha256": digest(output, deadline),
                    "size": info.st_size,
                }
                write_receipt(staging, receipt_name, receipt)
        except PublicationError:
            raise
        except InspectionError as error:
            raise PublicationError(str(error)) from error
    finally:
        os.close(output)
    sync_directory(stage)
    checkpoint("converted")


def stage_files(
    staging,
    stage,
    source,
    receipt_name,
    receipt,
    spec,
    deadline,
    checkpoint,
    *,
    should_continue=None,
    on_progress=None,
    pause_library_lock=nullcontext,
):
    stage_conversion(
        staging,
        stage,
        source,
        receipt_name,
        receipt,
        spec,
        deadline,
        checkpoint,
        should_continue=should_continue,
        on_progress=on_progress,
        pause_library_lock=pause_library_lock,
    )
    for file in spec.files:
        checkpoint("before-file")
        checked_source(source, file, deadline)
        try:
            with beneath(stage, file.name) as current:
                if (
                    os.fstat(current).st_size == file.identity["size"]
                    and digest(current, deadline) == file.sha256
                ):
                    if spec.mode == "hardlink" and not same_object(current, file.identity):
                        raise PublicationError("Staged media is not the expected hardlink")
                    continue
                owned = receipt.get("partial_files", {}).get(file.name)
                if not owned or not same_object(current, owned) or os.fstat(current).st_nlink != 1:
                    raise PublicationError("Existing staged file cannot be safely resumed")
            os.unlink(file.name, dir_fd=stage)
        except FileNotFoundError:
            pass
        if spec.mode == "hardlink":
            parent, _, basename = file.source.rpartition("/")
            with ExitStack() as stack:
                src_parent = (
                    stack.enter_context(beneath(source, parent, folder=True)) if parent else source
                )
                os.link(
                    basename,
                    file.name,
                    src_dir_fd=src_parent,
                    dst_dir_fd=stage,
                    follow_symlinks=False,
                )
            with beneath(stage, file.name) as linked:
                if not same_object(linked, file.identity):
                    raise PublicationError("Source changed while creating its hardlink")
        else:
            with beneath(source, file.source) as original:
                if not same_object(original, file.identity):
                    raise PublicationError("Source changed before copying")
                output = os.open(
                    file.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=stage,
                )
                try:
                    receipt.setdefault("partial_files", {})[file.name] = object_id(output)
                    write_receipt(staging, receipt_name, receipt)
                    checkpoint("copy-created")
                    while True:
                        if time.monotonic() > deadline:
                            raise PublicationError("Copy exceeded its time budget")
                        block = os.read(original, 1024 * 1024)
                        if not block:
                            break
                        write_all(output, block)
                    os.fsync(output)
                finally:
                    os.close(output)
        sync_directory(stage)
        checkpoint("file-staged")
    for name, content in generated_files(spec).items():
        try:
            output = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=stage
            )
        except FileExistsError:
            with beneath(stage, name) as fd:
                if digest(fd, deadline) != hashlib.sha256(content).hexdigest():
                    # Sidecar byte writes can be interrupted. Only remove a journaled own inode.
                    owned = receipt.get("partial_files", {}).get(name)
                    if not owned or not same_object(fd, owned) or os.fstat(fd).st_nlink != 1:
                        raise PublicationError(
                            "Existing sidecar conflicts with this import"
                        ) from None
                    os.unlink(name, dir_fd=stage)
                    output = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o644,
                        dir_fd=stage,
                    )
                else:
                    continue
        try:
            receipt.setdefault("partial_files", {})[name] = object_id(output)
            write_receipt(staging, receipt_name, receipt)
            write_all(output, content)
            os.fsync(output)
        finally:
            os.close(output)
    sync_directory(stage)


def conversion_reservation(spec):
    sources = conversion_inputs(spec)
    if not sources:
        return 0
    total = sum(chapter.identity["size"] for chapter in sources)
    overhead = min(8 * 1024 * 1024, max(256 * 1024, total // 20))
    return total + overhead


def _conversion_credit(stage, receipt, spec):
    plan = spec.conversion
    if plan is None:
        return 0
    name = plan.output_name
    try:
        with beneath(stage, name) as current:
            info = os.fstat(current)
            owned = (receipt or {}).get("partial_files", {}).get(name)
            if not owned or not same_object(current, owned) or info.st_nlink != 1:
                return 0
            recorded = (receipt or {}).get("derived", {}).get(name) or {}
            if recorded.get("size", 0) > 0 and info.st_size == recorded["size"]:
                return conversion_reservation(spec)
            return info.st_size
    except FileNotFoundError:
        return 0


def _require_free_space(staging, needed):
    space = os.fstatvfs(staging)
    if space.f_bavail * space.f_frsize < needed + 1024 * 1024:
        raise PublicationError("Not enough free space for this import")


def remaining_stage_bytes(staging, receipt, spec, deadline):
    if receipt and receipt.get("stage_identity"):
        try:
            with beneath(staging, receipt["stage_name"], folder=True) as stage:
                if same_object(stage, receipt["stage_identity"]):
                    verify_item(stage, spec, deadline, receipt.get("derived"), receipt)
                    return 0
        except (FileNotFoundError, PublicationError):
            pass  # Incomplete staging conservatively reserves a fresh complete copy.
    copied = sum(file.identity["size"] for file in spec.files) if spec.mode == "copy" else 0
    sidecars = sum(len(value) for value in generated_files(spec).values())
    credit = 0
    if receipt and receipt.get("stage_identity") and spec.conversion:
        try:
            with beneath(staging, receipt["stage_name"], folder=True) as stage:
                if same_object(stage, receipt["stage_identity"]):
                    credit = _conversion_credit(stage, receipt, spec)
        except FileNotFoundError:
            pass
    return max(0, sidecars + copied + conversion_reservation(spec) - credit)


def remaining_import_bytes(spec, *, timeout=600):
    """Read durable publication evidence before reserving additional storage."""
    deadline = time.monotonic() + timeout
    with private_staging(spec.staging_root) as staging, directory(spec.destination_root) as target:
        with publication_lock(staging, json.dumps(object_id(target), sort_keys=True)):
            receipt = read_receipt(staging, str(spec.entry_id) + ".json")
            if receipt:
                if receipt.get("spec_hash") != specification_fingerprint(spec) or receipt.get(
                    "destination_identity"
                ) != object_id(target):
                    raise PublicationError("Publication settings or destination identity changed")
            if spec.mode == "rename":
                return remaining_rename_bytes(target, receipt, spec, deadline)
            if receipt:
                try:
                    with beneath(target, spec.folder, folder=True) as existing:
                        if not receipt.get("stage_identity") or (
                            not same_object(existing, receipt["stage_identity"])
                            and not has_publication_marker(existing, receipt)
                        ):
                            raise PublicationError("Destination belongs to another item")
                        verify_item(existing, spec, deadline, receipt.get("derived"), receipt)
                        return 0
                except FileNotFoundError:
                    pass
            return remaining_stage_bytes(staging, receipt, spec, deadline)


def seeding_copy_bytes(library_device, spec) -> int:
    """Bytes qBittorrent copies when the download and library are different filesystems."""
    if all(file.identity["device"] == library_device for file in spec.files):
        return 0
    if spec.source_kind == "file":
        return sum(file.identity["size"] for file in spec.files)
    try:
        with directory(spec.source_root) as root:
            with beneath(root, spec.source_relative, folder=True) as folder:
                return sum(info["size"] for _, info in enumerate_files(folder))
    except (FileNotFoundError, InspectionError, OSError):
        return sum(file.identity["size"] for file in spec.files)


def remaining_rename_bytes(target, receipt, spec, deadline):
    sidecars = sum(len(value) for value in generated_files(spec).values())
    copy = seeding_copy_bytes(os.fstat(target).st_dev, spec)
    try:
        with beneath(target, spec.folder, folder=True) as existing:
            if (
                receipt
                and receipt.get("stage_identity")
                and not same_object(existing, receipt["stage_identity"])
            ):
                raise PublicationError("Destination belongs to another item")
            try:
                verify_item(existing, spec, deadline)
            except PublicationError:
                return sidecars + copy
            return 0
    except FileNotFoundError:
        return sidecars + copy


def load_rename_plan(spec):
    with private_staging(spec.staging_root) as staging, directory(spec.destination_root) as target:
        with publication_lock(staging, json.dumps(object_id(target), sort_keys=True)):
            receipt = read_receipt(staging, str(spec.entry_id) + ".json")
            if receipt is None or "rename_plan" not in receipt:
                return None
            if receipt.get("spec_hash") != specification_fingerprint(spec) or receipt.get(
                "destination_identity"
            ) != object_id(target):
                raise PublicationError("Publication settings or destination identity changed")
            return receipt["rename_plan"]


def remember_rename_plan(spec, plan):
    spec_hash = specification_fingerprint(spec)
    receipt_name = str(spec.entry_id) + ".json"
    with private_staging(spec.staging_root) as staging, directory(spec.destination_root) as target:
        if same_object(staging, object_id(target)):
            raise PublicationError("Staging and library refer to the same directory")
        with publication_lock(staging, json.dumps(object_id(target), sort_keys=True)):
            receipt = read_receipt(staging, receipt_name)
            if receipt is None:
                receipt = {
                    "schema_version": 1,
                    "entry_id": str(spec.entry_id),
                    "spec_hash": spec_hash,
                    "stage_name": "rename-" + spec.entry_id.hex,
                    "state": "preparing",
                    "destination_identity": object_id(target),
                    "rename_plan": plan,
                }
                write_receipt(staging, receipt_name, receipt, create=True)
                return plan
            if receipt.get("spec_hash") != spec_hash or receipt.get(
                "destination_identity"
            ) != object_id(target):
                raise PublicationError("Publication settings or destination identity changed")
            stored = receipt.get("rename_plan")
            if stored and stored != plan:
                raise PublicationError(
                    "The seeding rename plan changed after qBittorrent was asked to move files"
                )
            receipt["rename_plan"] = plan
            write_receipt(staging, receipt_name, receipt)
            return plan


def write_generated(folder, staging, receipt_name, receipt, spec, deadline):
    for name, content in generated_files(spec).items():
        try:
            output = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=folder
            )
        except FileExistsError:
            with beneath(folder, name) as fd:
                if digest(fd, deadline) != hashlib.sha256(content).hexdigest():
                    owned = receipt.get("partial_files", {}).get(name)
                    if not owned or not same_object(fd, owned) or os.fstat(fd).st_nlink != 1:
                        raise PublicationError(
                            "Existing sidecar conflicts with this import"
                        ) from None
                    os.unlink(name, dir_fd=folder)
                    output = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o644,
                        dir_fd=folder,
                    )
                else:
                    continue
        try:
            receipt.setdefault("partial_files", {})[name] = object_id(output)
            write_receipt(staging, receipt_name, receipt)
            write_all(output, content)
            os.fsync(output)
        finally:
            os.close(output)
    sync_directory(folder)


def publish_renamed(
    spec: PublicationSpec,
    *,
    checkpoint: Callable[[str], None] = lambda _: None,
    timeout=600,
    publication_guard: Callable = nullcontext,
):
    """Verify files qBittorrent already placed, then write sidecars beside them."""
    deadline = time.monotonic() + timeout
    spec_hash = specification_fingerprint(spec)
    receipt_name = str(spec.entry_id) + ".json"
    with (
        private_staging(spec.staging_root) as staging,
        directory(spec.destination_root) as destination,
    ):
        if same_object(staging, object_id(destination)):
            raise PublicationError("Staging and library refer to the same directory")
        with publication_lock(staging, json.dumps(object_id(destination), sort_keys=True)):
            receipt = read_receipt(staging, receipt_name)
            if receipt is None:
                receipt = {
                    "schema_version": 1,
                    "entry_id": str(spec.entry_id),
                    "spec_hash": spec_hash,
                    "stage_name": "rename-" + spec.entry_id.hex,
                    "state": "preparing",
                    "destination_identity": object_id(destination),
                }
                write_receipt(staging, receipt_name, receipt, create=True)
            if receipt.get("spec_hash") != spec_hash or receipt.get(
                "destination_identity"
            ) != object_id(destination):
                raise PublicationError("Publication settings or destination identity changed")
            if receipt["state"] in {"cancelling", "cancelled"}:
                raise PublicationError("This import was cancelled; review a new plan")
            try:
                with beneath(destination, spec.folder, folder=True) as leaf:
                    if receipt.get("stage_identity") and not same_object(
                        leaf, receipt["stage_identity"]
                    ):
                        raise PublicationError("Destination exists and belongs to another item")
                    verify_item_media(leaf, spec, deadline)
                    if not receipt.get("stage_identity"):
                        receipt["stage_identity"] = object_id(leaf)
                        write_receipt(staging, receipt_name, receipt)
                    with publication_guard():
                        checkpoint("before-publish")
                        with directory(spec.destination_root) as current:
                            if not same_object(current, receipt["destination_identity"]):
                                raise PublicationError(
                                    "Destination mount changed before publication"
                                )
                        write_generated(leaf, staging, receipt_name, receipt, spec, deadline)
                        verify_item(leaf, spec, deadline)
                        checkpoint("published-before-receipt")
                        receipt["state"] = "published"
                        write_receipt(staging, receipt_name, receipt)
                        return receipt
            except FileNotFoundError:
                raise PublicationError(
                    "qBittorrent has not placed the renamed files in the library folder"
                ) from None


def verify_item_media(folder, spec, deadline):
    expected = {file.name for file in spec.files}
    allowed = expected | set(generated_files(spec))
    present = leaf_names(folder, spec)
    if present - allowed:
        raise PublicationError("The library folder already contains other files")
    if not expected <= present:
        raise PublicationError(
            "qBittorrent has not finished renaming every file into the library folder"
        )
    for file in spec.files:
        with beneath(folder, file.name) as fd:
            if os.fstat(fd).st_size != file.identity["size"] or digest(fd, deadline) != file.sha256:
                raise PublicationError("The renamed file does not match the downloaded bytes")
            if not seeding_same_file(fd, file.identity):
                raise PublicationError("The library file is not the seeding copy")


def publish_item(
    spec: PublicationSpec,
    *,
    checkpoint: Callable[[str], None] = lambda _: None,
    timeout=600,
    publication_guard: Callable = nullcontext,
    should_continue: Callable[[], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
):
    if spec.mode == "rename":
        return publish_renamed(
            spec, checkpoint=checkpoint, timeout=timeout, publication_guard=publication_guard
        )
    deadline = time.monotonic() + timeout
    spec_hash = specification_fingerprint(spec)
    receipt_name = str(spec.entry_id) + ".json"
    with (
        private_staging(spec.staging_root) as staging,
        directory(spec.destination_root) as destination,
    ):
        if same_object(staging, object_id(destination)):
            raise PublicationError("Staging and library refer to the same directory")
        with (
            entry_lock(staging, spec.entry_id),
            publication_lock(staging, json.dumps(object_id(destination), sort_keys=True)) as pause,
        ):
            receipt = read_receipt(staging, receipt_name)
            if receipt is None:
                receipt = {
                    "schema_version": 1,
                    "entry_id": str(spec.entry_id),
                    "spec_hash": spec_hash,
                    "stage_name": "item-" + uuid4().hex,
                    "state": "preparing",
                    "destination_identity": object_id(destination),
                }
                write_receipt(staging, receipt_name, receipt, create=True)
            if receipt.get("spec_hash") != spec_hash or receipt.get(
                "destination_identity"
            ) != object_id(destination):
                raise PublicationError("Publication settings or destination identity changed")
            if receipt["state"] in {"cancelling", "cancelled"}:
                raise PublicationError("This import was cancelled; review a new plan")
            # Inspect an existing leaf before touching source or staging: a previous publication
            # may have succeeded even when DB acknowledgement or receipt update was interrupted.
            try:
                with beneath(destination, spec.folder, folder=True) as existing:
                    if not receipt.get("stage_identity") or (
                        not same_object(existing, receipt["stage_identity"])
                        and not has_publication_marker(existing, receipt)
                    ):
                        raise PublicationError("Destination exists and belongs to another item")
                    verify_item(existing, spec, deadline, receipt.get("derived"), receipt)
                    receipt["stage_identity"] = object_id(existing)
                    receipt["state"] = "published"
                    write_receipt(staging, receipt_name, receipt)
                    remove_publication_marker(existing, receipt)
                    return receipt
            except FileNotFoundError:
                if receipt["state"] == "published":
                    raise PublicationError(
                        "Previously published item is missing; resolve before replacing it"
                    ) from None
            with (
                directory(spec.source_root) as source_root,
                source_scope(source_root, spec.source_relative, spec.source_kind) as source,
            ):
                if not same_object(source, spec.source_directory):
                    raise PublicationError("Completed-download directory identity changed")
                for file in (*spec.files, *conversion_inputs(spec)):
                    checked_source(source, file, deadline)
                needed = remaining_stage_bytes(staging, receipt, spec, deadline)
                _require_free_space(staging, needed)
                prepare_stage(staging, receipt_name, receipt, spec)
                checkpoint("stage-created")
                with beneath(staging, receipt["stage_name"], folder=True) as stage:
                    stage_files(
                        staging,
                        stage,
                        source,
                        receipt_name,
                        receipt,
                        spec,
                        deadline,
                        checkpoint,
                        should_continue=should_continue,
                        on_progress=on_progress,
                        pause_library_lock=pause,
                    )
                    verify_item(stage, spec, deadline, receipt.get("derived"), receipt)
                    for file in (*spec.files, *conversion_inputs(spec)):
                        checked_source(source, file, deadline)
                    receipt["state"] = "prepared"
                    write_receipt(staging, receipt_name, receipt)
                    checkpoint("prepared")
                    with destination_parent(destination, spec.folder) as (parent, leaf):
                        if conflicting_name(parent, leaf) is not None:
                            raise PublicationError("Destination name is already occupied")
                        with publication_guard():
                            # Recheck current permissions/lease immediately before rename.
                            checkpoint("before-publish")
                            with directory(spec.destination_root) as current:
                                if not same_object(current, receipt["destination_identity"]):
                                    raise PublicationError(
                                        "Destination mount changed before publication"
                                    )
                            relative_parent = str(PurePosixPath(spec.folder).parent)
                            with ExitStack() as recheck:
                                current_parent = (
                                    recheck.enter_context(
                                        beneath(destination, relative_parent, folder=True)
                                    )
                                    if relative_parent != "."
                                    else destination
                                )
                                if object_id(current_parent) != object_id(parent):
                                    raise PublicationError(
                                        "Destination parent moved before publication"
                                    )
                                current_stage = recheck.enter_context(
                                    beneath(staging, receipt["stage_name"], folder=True)
                                )
                                if not same_object(current_stage, receipt["stage_identity"]):
                                    raise PublicationError(
                                        "Staged directory changed before publication"
                                    )
                            no_replace(staging, receipt["stage_name"], parent, leaf)
                            sync_directory(parent)
                            sync_directory(staging)
                            checkpoint("published-before-receipt")
                            with beneath(parent, leaf, folder=True) as published:
                                if not has_publication_marker(published, receipt):
                                    raise PublicationError(
                                        "Published directory lost its ownership marker"
                                    )
                                receipt["stage_identity"] = object_id(published)
                                receipt["state"] = "published"
                                write_receipt(staging, receipt_name, receipt)
                                remove_publication_marker(published, receipt)
                                return receipt


def _mounts(mountinfo="/proc/self/mountinfo"):
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return []
    mounts = []
    for line in lines:
        fields = line.split(" ")
        if "-" not in fields[6:]:
            continue
        separator = fields.index("-", 6)
        point = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
        options = {*fields[5].split(","), *fields[separator + 3].split(",")}
        mounts.append((Path(point), fields[separator + 1], options))
    return mounts


def mount_warnings(*paths, mountinfo="/proc/self/mountinfo"):
    """Mount options that break the device/inode identity checks publication depends on."""
    mounts = _mounts(mountinfo)
    warnings = []
    for path in paths:
        containing = [mount for mount in mounts if path.is_relative_to(mount[0])]
        if not containing:
            continue
        point, kind, options = max(containing, key=lambda mount: len(mount[0].parts))
        warning = (
            f"{point} is an SMB mount using noserverino, so file identities can change "
            "between checks and imports may be held. Remount it with serverino."
        )
        if kind in {"cifs", "smb3"} and "noserverino" in options and warning not in warnings:
            warnings.append(warning)
    return warnings


def probe_download_folder(
    source_root: Path, relative: str, destination_root: Path, staging_root: Path
):
    """Use an owned temporary file to qualify an empty downloader save folder."""
    if any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for left, right in (
            (source_root, destination_root),
            (source_root, staging_root),
            (destination_root, staging_root),
        )
    ):
        raise PublicationError("Source, staging and library roots must not overlap")
    if relative:
        relative_parts(relative)
    name = f".book-search-route-{uuid4().hex}.tmp"
    content = b"book-search temporary route test\n"
    with directory(source_root) as root, ExitStack() as handles:
        parent = handles.enter_context(beneath(root, relative, folder=True)) if relative else root
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            owned = object_id(fd)
            try:
                write_all(fd, content)
                os.fsync(fd)
                source = f"{relative}/{name}" if relative else name
                return probe_destination(
                    source_root,
                    source,
                    PublishFile(
                        source=name,
                        name="probe",
                        sha256=hashlib.sha256(content).hexdigest(),
                        identity=identity(os.fstat(fd)),
                    ),
                    destination_root,
                    staging_root,
                    source_kind="file",
                )
            finally:
                try:
                    observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if {"device": observed.st_dev, "inode": observed.st_ino} != owned:
                        raise PublicationError(
                            "Setup probe changed; unrecognized replacement preserved"
                        )
                    os.unlink(name, dir_fd=parent)
                    sync_directory(parent)
        finally:
            os.close(fd)


def probe_destination(
    source_root: Path,
    source_relative: str,
    file: PublishFile,
    destination_root: Path,
    staging_root: Path,
    *,
    source_kind: Literal["directory", "file"] = "directory",
):
    """Probe an actual selected file's link route, no-replace renames and staging locks."""
    if source_kind == "file" and file.source != relative_parts(source_relative)[-1]:
        raise PublicationError("A single-file probe must use its selected file")
    if any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for left, right in (
            (source_root, destination_root),
            (source_root, staging_root),
            (destination_root, staging_root),
        )
    ):
        raise PublicationError("Source, staging and library roots must not overlap")
    token = uuid4().hex
    staged, target, linked = f"probe-{token}", f".book-search-probe-{token}", f"link-{token}"
    report = {"hardlink": False, "copy": False, "no_replace": False}
    created = dict.fromkeys(
        ("link", "stage", "target", "marker", "write", "claim", "claimed", "lock")
    )
    write_name, marker, lock_name = "write-" + token, "marker", "lock-probe-" + token
    marker_content = f"book-search destination probe {token}\n".encode()
    claim_name, claimed_name = "publish-" + token, "published-" + token
    target_handle = None
    exclusive = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

    def refused(operation):
        try:
            operation()
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            return True
        return False

    def has_marker(folder):
        try:
            with beneath(folder, marker) as current:
                info = os.fstat(current)
                if info.st_size != len(marker_content):
                    return False
                os.lseek(current, 0, os.SEEK_SET)
                return os.read(current, len(marker_content) + 1) == marker_content
        except (FileNotFoundError, InspectionError):
            return False

    with (
        directory(source_root) as source_mount,
        source_scope(source_mount, source_relative, source_kind) as source,
        private_staging(staging_root) as staging,
        directory(destination_root) as destination,
        ExitStack() as handles,
    ):
        checked_source(source, file, time.monotonic() + 120)
        if same_object(staging, object_id(destination)):
            raise PublicationError("Staging and library refer to the same directory")
        report.update(
            destination_identity=object_id(destination), staging_identity=object_id(staging)
        )
        try:
            output = os.open(
                write_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=staging,
            )
            created["write"] = object_id(output)
            handles.callback(os.close, os.dup(output))
            try:
                write_all(output, b"book-search destination probe\n")
                os.fsync(output)
            finally:
                os.close(output)
            with beneath(staging, write_name) as checked:
                report["copy"] = os.read(checked, 100) == b"book-search destination probe\n"
            parent, _, name = file.source.rpartition("/")
            with ExitStack() as stack:
                source_parent = (
                    stack.enter_context(beneath(source, parent, folder=True)) if parent else source
                )
                try:
                    os.link(
                        name,
                        linked,
                        src_dir_fd=source_parent,
                        dst_dir_fd=staging,
                        follow_symlinks=False,
                    )
                    linked_info = os.stat(linked, dir_fd=staging, follow_symlinks=False)
                    created["link"] = {
                        "device": linked_info.st_dev,
                        "inode": linked_info.st_ino,
                    }
                    with beneath(staging, linked) as fd:
                        report["hardlink"] = same_object(fd, file.identity)
                except OSError as error:
                    report["hardlink_error"] = errno.errorcode.get(error.errno, "IO_ERROR")
            # Publication journals are created with a file no-replace rename inside staging.
            claim = os.open(claim_name, exclusive, 0o600, dir_fd=staging)
            created["claim"] = object_id(claim)
            os.close(claim)
            report["receipt_mode"] = no_replace(staging, claim_name, staging, claimed_name)
            created["claimed"], created["claim"] = created["claim"], None
            claim = os.open(claim_name, exclusive, 0o600, dir_fd=staging)
            created["claim"] = object_id(claim)
            os.close(claim)
            if not refused(lambda: no_replace(staging, claim_name, staging, claimed_name)):
                created["claimed"], created["claim"] = created["claim"], None
                raise PublicationError("Staging filesystem replaced an existing journal")
            lock = os.open(lock_name, exclusive, 0o600, dir_fd=staging)
            created["lock"] = object_id(lock)
            handles.callback(os.close, lock)
            _acquire(lock, "Another probe holds this lock")
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.mkdir(staged, mode=0o700, dir_fd=staging)
            stage_handle = handles.enter_context(beneath(staging, staged, folder=True))
            created["stage"] = object_id(stage_handle)
            report["no_replace_mode"] = no_replace(staging, staged, destination, target)
            created["target"], created["stage"] = created["stage"], None
            target_handle = stage_handle
            output = os.open(marker, exclusive, 0o600, dir_fd=target_handle)
            created["marker"] = object_id(output)
            try:
                write_all(output, marker_content)
                os.fsync(output)
            finally:
                os.close(output)
            # Some FUSE and network filesystems report a different inode for a directory
            # after it is renamed. Recognize the random marker through the destination
            # name before accepting the post-rename identity.
            with beneath(destination, target, folder=True) as current_target:
                if not has_marker(current_target):
                    raise PublicationError(
                        "Probe object changed; unrecognized replacement preserved"
                    )
                created["target"] = object_id(current_target)
            os.mkdir(staged, mode=0o700, dir_fd=staging)
            stage_handle = handles.enter_context(beneath(staging, staged, folder=True))
            created["stage"] = object_id(stage_handle)
            report["no_replace"] = refused(lambda: no_replace(staging, staged, destination, target))
            if report["no_replace"]:
                # The fallback relies on rename(2) refusing a non-empty directory.
                with beneath(destination, target, folder=True) as current_target:
                    if not has_marker(current_target):
                        raise PublicationError(
                            "Probe object changed; unrecognized replacement preserved"
                        )
                    created["target"] = object_id(current_target)
                report["no_replace"] = refused(
                    lambda: os.rename(staged, target, src_dir_fd=staging, dst_dir_fd=destination)
                )
            if not report["no_replace"]:
                raise PublicationError("Filesystem failed the no-replace collision probe")
            sync_directory(destination)
            sync_directory(staging)
            space = os.fstatvfs(destination)
            report["available_bytes"] = space.f_bavail * space.f_frsize
            report["warnings"] = mount_warnings(destination_root, staging_root)
            return report
        finally:
            changed = False
            for fd, name, folder, owned in (
                (staging, linked, False, created["link"]),
                (staging, staged, True, created["stage"]),
                (staging, write_name, False, created["write"]),
                (staging, claim_name, False, created["claim"]),
                (staging, claimed_name, False, created["claimed"]),
                (staging, lock_name, False, created["lock"]),
            ):
                if not owned:
                    continue
                try:
                    observed = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if {"device": observed.st_dev, "inode": observed.st_ino} != owned:
                        changed = True
                        continue
                    (os.rmdir if folder else os.unlink)(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
            marker_removed = False
            if created["target"]:
                try:
                    with beneath(destination, target, folder=True) as current_target:
                        if created["marker"] and has_marker(current_target):
                            os.unlink(marker, dir_fd=current_target)
                            marker_removed = True
                            os.rmdir(target, dir_fd=destination)
                        elif object_id(current_target) == created["target"]:
                            os.rmdir(target, dir_fd=destination)
                        else:
                            changed = True
                except FileNotFoundError:
                    pass
                except OSError:
                    changed = True
            # If the probe directory was moved away and replaced, its open descriptor
            # still lets us remove only our random marker while preserving both folders.
            if target_handle is not None and created["marker"] and not marker_removed:
                try:
                    if has_marker(target_handle):
                        os.unlink(marker, dir_fd=target_handle)
                    else:
                        changed = True
                except FileNotFoundError:
                    pass
            if changed:
                raise PublicationError("Probe object changed; unrecognized replacement preserved")
