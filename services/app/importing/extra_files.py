"""Non-book companions that do not require an import decision."""

from pathlib import PurePosixPath

EXTRA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".cue",
    ".nfo",
    ".txt",
    ".sfv",
    ".m3u",
    ".m3u8",
    ".opf",
}


def is_extra(path):
    return PurePosixPath(path).suffix.lower() in EXTRA_EXTENSIONS
