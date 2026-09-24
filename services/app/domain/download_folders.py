"""Read-only browsing of download volumes visible to this Dewarr instance."""

import os
import re
from pathlib import Path

from fastapi import HTTPException

# Never offer runtime, configuration or operating-system mounts as download volumes.
_SYSTEM_ROOTS = (
    "/bin",
    "/boot",
    "/config",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/tmp",
    "/usr",
    "/var",
)
MAX_ENTRIES = 500


def volume_roots(mountinfo=Path("/proc/self/mountinfo")):
    roots = {Path("/data")}
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        path = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
        if path == Path("/") or any(path.is_relative_to(root) for root in _SYSTEM_ROOTS):
            continue
        roots.add(path)
    return roots


def browse_roots(settings, *, include_libraries=False):
    candidates = volume_roots() | set(settings.import_sources.values())
    if include_libraries:
        candidates.update(settings.import_destinations.values())
    roots = set()
    for path in candidates:
        try:
            resolved = path.resolve(strict=True)
            if (
                resolved != Path("/")
                and resolved.is_dir()
                and os.access(resolved, os.R_OK | os.X_OK)
            ):
                roots.add(resolved)
        except (OSError, RuntimeError):
            continue
    # One top-level entry per tree; children remain browsable.
    return sorted(
        root
        for root in roots
        if not any(root != other and root.is_relative_to(other) for other in roots)
    )


def readable_folder(path, roots):
    try:
        resolved = Path(path).resolve(strict=True)
        return (
            Path(path) == resolved
            and any(resolved.is_relative_to(root) for root in roots)
            and resolved.is_dir()
            and os.access(resolved, os.R_OK | os.X_OK)
        )
    except (OSError, RuntimeError):
        return False


def browse_folders(settings, path=None, *, include_libraries=False):
    roots = browse_roots(settings, include_libraries=include_libraries)
    if path is None:
        return {
            "path": None,
            "parent": None,
            "directories": [str(root) for root in roots],
            "truncated": False,
        }
    if not readable_folder(path, roots):
        raise HTTPException(422, "Choose a readable folder inside a mounted volume.")
    current = Path(path).resolve(strict=True)
    directories = []
    try:
        with os.scandir(current) as entries:
            for entry in entries:
                # Do not traverse symlinks into other volumes or reveal hidden folders.
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(str(current / entry.name))
                    if len(directories) > MAX_ENTRIES:
                        break
    except OSError as error:
        raise HTTPException(
            422, "Dewarr cannot read this folder. Check its mount and permissions."
        ) from error
    parent = (
        str(current.parent) if any(current.parent.is_relative_to(root) for root in roots) else None
    )
    return {
        "path": str(current),
        "parent": parent,
        "directories": sorted(directories[:MAX_ENTRIES]),
        "truncated": len(directories) > MAX_ENTRIES,
    }
