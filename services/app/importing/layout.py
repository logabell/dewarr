"""The sole permitted library/staging overlap is our reserved hidden child."""

from pathlib import Path

STAGING_NAME = ".book-search-staging"


def overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def unsafe_staging(library: Path, staging: Path) -> bool:
    return overlaps(library, staging) and staging != library / STAGING_NAME


def unsafe_roots(source: Path, library: Path, staging: Path) -> bool:
    return (
        overlaps(source, library) or overlaps(source, staging) or unsafe_staging(library, staging)
    )


def library_relative(value: str) -> None:
    # Reserve this namespace even on case-insensitive media shares. Import and
    # combine targets/sources must never address journals or incomplete media.
    if value.split("/", 1)[0].casefold() == STAGING_NAME.casefold():
        raise ValueError("The private staging folder is reserved; choose a library item path")


def check_staging_backend(kind: str, library: Path, staging: Path, *, watcher_enabled=None) -> None:
    if (
        overlaps(library, staging)
        and kind != "audiobookshelf"
        and not (kind == "grimmory" and watcher_enabled is False)
    ):
        raise ValueError(
            "Grimmory's watcher does not reliably exclude hidden staging folders. "
            "Disable its folder watcher and allow library scans for this library-only mount, "
            "or mount a shared parent so staging can stay outside the library."
        )
