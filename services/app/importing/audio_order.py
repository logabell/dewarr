"""Conservative filename ordering for untagged recordings."""

import re
from pathlib import PurePosixPath


def numbered_sequence(paths):
    """Return positions only for one flat, unique sequence from 1 through N.

    This establishes order, not identity or completeness of the source release.
    """
    paths = [PurePosixPath(path) for path in paths]
    if len(paths) < 2 or len({path.parent for path in paths}) != 1:
        return []
    matches = [re.fullmatch(r"(?:track[ _-]*)?(\d{1,3})", path.stem, re.I) for path in paths]
    if not all(matches):
        return []
    numbers = [int(match[1]) for match in matches]
    return numbers if sorted(numbers) == list(range(1, len(paths) + 1)) else []


def inferred_tracks(files):
    positions = numbered_sequence(file["path"] for file in files)
    if positions and all(
        file.get("disc") in (None, 1) and file.get("track") in (None, position)
        for file, position in zip(files, positions, strict=True)
    ):
        return [
            {**file, "track": position} for file, position in zip(files, positions, strict=True)
        ]
    return files
