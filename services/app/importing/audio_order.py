"""Conservative filename ordering for untagged recordings."""

import re
from pathlib import PurePosixPath

from app.domain.audio_manifest import numbered_positions


def numbered_sequence(paths, *, title=None):
    """Return positions only for one flat, unique sequence from 1 through N.

    This establishes order, not identity or completeness of the source release.
    """
    paths = [PurePosixPath(path) for path in paths]
    if len(paths) < 2 or len({path.parent for path in paths}) != 1:
        return []
    positions = numbered_positions((path.stem for path in paths), title or "")
    if not positions:
        named = [
            re.fullmatch(r"(.+?)\s+(\d{1,3})\s+of\s+(\d{1,3})", path.stem, re.I) for path in paths
        ]
        if (
            not all(named)
            or len({match[1].casefold() for match in named}) != 1
            or any(int(match[3]) != len(paths) for match in named)
        ):
            return []
        numbers = [int(match[2]) for match in named]
        return numbers if sorted(numbers) == list(range(1, len(paths) + 1)) else []
    if any(disc != 1 for disc, _ in positions):
        return []
    numbers = [track for _, track in positions]
    return numbers if sorted(numbers) == list(range(1, len(paths) + 1)) else []


def inferred_tracks(files, *, title=None):
    positions = numbered_sequence((file["path"] for file in files), title=title)
    if positions and all(
        file.get("disc") in (None, 1) and file.get("track") in (None, position)
        for file, position in zip(files, positions, strict=True)
    ):
        return [
            {**file, "track": position} for file, position in zip(files, positions, strict=True)
        ]
    return files
