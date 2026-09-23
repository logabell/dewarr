"""Read-only descriptor-relative access to explicitly mounted download trees."""

import errno
import hashlib
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from app.importing.naming import PlannedSourceFile


class InspectionError(ValueError):
    pass


def relative_parts(value: str) -> list[str]:
    PlannedSourceFile(path=value)
    return value.split("/")


@contextmanager
def directory(path: Path):
    # A leading "//" is a UNC prefix. Walking parts[1:] would open /host/share.
    if not path.is_absolute() or path.anchor != "/" or str(path) == "/" or ".." in path.parts:
        raise InspectionError("Configure an absolute download root, not the filesystem root")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def beneath(root: int, relative: str, *, folder=False):
    fd = os.dup(root)
    try:
        parts = relative_parts(relative)
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if folder or index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) if folder else stat.S_ISREG(info.st_mode)):
            raise InspectionError("Only regular files and directories can be inspected")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def source_scope(root: int, relative: str, kind: str):
    """Open the source directory without broadening a single-file selection."""
    parts = relative_parts(relative)
    if kind == "directory":
        with beneath(root, relative, folder=True) as fd:
            yield fd
    elif kind == "file":
        if len(parts) > 1:
            with beneath(root, "/".join(parts[:-1]), folder=True) as fd:
                yield fd
        else:
            fd = os.dup(root)
            try:
                yield fd
            finally:
                os.close(fd)
    else:
        raise InspectionError("Unknown source inspection scope")


def describe_os_error(error: OSError, path: Path | None = None) -> str:
    """Explain a mount or permission failure in terms a Docker user can act on."""
    subject = str(path) if path else "a folder"
    denied = (
        f"Dewarr (uid {os.geteuid()}) was denied access to {subject}. Give PUID/PGID read "
        "and write access to the library, staging and download folders."
    )
    messages = {
        errno.EACCES: denied,
        errno.EPERM: denied,
        errno.ENOENT: f"Dewarr cannot find {subject} inside its container. "
        "Check the volume mounts.",
        errno.ENOTDIR: f"Dewarr expected {subject} to be a folder, "
        "but part of that path is a file.",
        errno.ELOOP: f"The path to {subject} contains a symbolic link, which Dewarr does not "
        "follow. Enter the real folder path.",
        errno.EXDEV: "The staging folder and library folder are on different filesystems. "
        "Mount the folder that contains both into Dewarr.",
        errno.EROFS: f"Dewarr cannot write to {subject} because it is mounted read-only. "
        "Remove :ro from that volume.",
        errno.ENOSPC: f"The filesystem holding {subject} is full.",
        errno.EDQUOT: f"The filesystem holding {subject} is over its quota.",
    }
    message = messages.get(error.errno) or (
        f"{f'Dewarr could not use {path}' if path else 'Destination probe failed'}; "
        "check paths, permissions and filesystem support"
    )
    code = errno.errorcode.get(error.errno)
    return f"{message} ({code})" if code else message


def identity(info):
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def digest(fd, deadline):
    os.lseek(fd, 0, os.SEEK_SET)
    result = hashlib.sha256()
    while True:
        if time.monotonic() > deadline:
            raise InspectionError("Inspection exceeded its time budget; reduce the batch size")
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        result.update(block)
    os.lseek(fd, 0, os.SEEK_SET)
    return result.hexdigest()


def enumerate_files(root, *, max_entries=10000, max_depth=20):
    files = []
    visited = 0

    def walk(fd, parent, depth):
        nonlocal visited
        if depth > max_depth:
            raise InspectionError("Download directory exceeds the supported depth")
        before = identity(os.fstat(fd))
        with os.scandir(fd) as entries:
            for entry in entries:
                visited += 1
                if visited > max_entries:
                    raise InspectionError("Download exceeds the supported entry count")
                relative = f"{parent}/{entry.name}" if parent else entry.name
                relative_parts(relative)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    with beneath(fd, entry.name, folder=True) as child:
                        if identity(os.fstat(child)) != identity(info):
                            raise InspectionError("Directory changed during inspection")
                        walk(child, relative, depth + 1)
                elif stat.S_ISREG(info.st_mode):
                    files.append((relative, identity(info)))
                else:
                    raise InspectionError("Download contains a symlink or special file")
        if before != identity(os.fstat(fd)):
            raise InspectionError("Directory changed during inspection")

    walk(root, "", 0)
    return sorted(files)
