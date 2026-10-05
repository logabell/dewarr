"""Bounded, descriptor-relative browsing of configured read-only download roots."""

import os
import stat
from contextlib import nullcontext
from pathlib import Path

from app.importing.filesystem import beneath, directory


def browse_downloads(root: Path, path: str = "", *, limit: int = 500):
    entries = []
    truncated = False
    with directory(root) as root_fd:
        with beneath(root_fd, path, folder=True) if path else nullcontext(root_fd) as fd:
            with os.scandir(fd) as listing:
                for index, entry in enumerate(listing):
                    if index >= limit:
                        truncated = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        continue
                    entries.append(
                        {
                            "name": entry.name,
                            "path": f"{path}/{entry.name}" if path else entry.name,
                            "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                            "size": info.st_size if stat.S_ISREG(info.st_mode) else None,
                        }
                    )
    entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold()))
    return {
        "path": path,
        "parent": path.rpartition("/")[0] if path else None,
        "entries": entries,
        "truncated": truncated,
    }
