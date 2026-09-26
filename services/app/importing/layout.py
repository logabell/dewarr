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


def check_library_layout(roots: dict[str, Path], sources: dict[str, Path], staging: Path):
    """Explain the actual conflicting locations; folder names have no media meaning."""
    for key, root in roots.items():
        for source_key, source in sources.items():
            if overlaps(root, source):
                raise ValueError(
                    f'Library folder "{key}" ({root}) overlaps download folder '
                    f'"{source_key}" ({source}). Choose separate locations or correct '
                    "the download path mapping in Settings → Download clients. "
                    "Also check BOOK_IMPORT_SOURCES if configured."
                )
        if unsafe_staging(root, staging):
            raise ValueError(
                f'Staging folder {staging} overlaps library folder "{key}" ({root}). '
                f"Only its direct {STAGING_NAME} child may be used inside a library. "
                "Check BOOK_IMPORT_STAGING_ROOT if configured."
            )
    for key, source in sources.items():
        if overlaps(source, staging):
            raise ValueError(
                f'Staging folder {staging} overlaps download folder "{key}" ({source}). '
                "Keep staging separate from downloads; check BOOK_IMPORT_STAGING_ROOT "
                "and the download path mapping."
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
