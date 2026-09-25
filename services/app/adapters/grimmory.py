"""Grimmory library client.

Inventory and import talk to Grimmory the way they talk to Audiobookshelf:
paged books become the same item evidence, and a verified folder plus refresh
or watch is how completed downloads show up. Playback stays in Grimmory.
"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import tempfile
from pathlib import PurePosixPath

import httpx
from pydantic import BaseModel, Field

from app.adapters.audiobookshelf import (
    ABSFile,
    ABSItem,
    backend_path,
    external_id,
    folder_title,
    issue_preview,
    names,
    parse_reason,
)
from app.adapters.batching import read_batches
from app.adapters.contracts import AdapterError, Capabilities, FailureKind
from app.adapters.http import JsonEndpoint
from app.domain.catalog_language import catalog_language
from app.importing.metadata import valid_isbn

logger = logging.getLogger(__name__)

EBOOK_TYPES = {
    "PDF": "pdf",
    "EPUB": "epub",
    "MOBI": "mobi",
    "AZW3": "azw3",
    "FB2": "fb2",
    "CBX": "cbz",
}
EBOOK_FORMATS = set(EBOOK_TYPES.values()) | {"cbr", "cbz"}
AUDIO_EXTENSIONS = {"m4b", "m4a", "mp3", "opus"}
# Grimmory's allowedFormats field names book types, not extensions.
FORMAT_TYPES = {
    "pdf": "PDF",
    "epub": "EPUB",
    "mobi": "MOBI",
    "azw3": "AZW3",
    "azw": "AZW3",
    "fb2": "FB2",
    "cbz": "CBX",
    "cbr": "CBX",
    "cb7": "CBX",
    "m4b": "AUDIOBOOK",
    "m4a": "AUDIOBOOK",
    "mp3": "AUDIOBOOK",
    "opus": "AUDIOBOOK",
}
CATALOG_PAGE_BYTES = 32 * 1024 * 1024


class GrimmoryImportConfiguration(BaseModel):
    library_id: str
    folders: list[str]
    audiobooks_only: bool
    audio_allowed: bool
    watcher_enabled: bool
    organization_mode: str
    metadata_source: str
    allowed_formats: list[str] = Field(default_factory=list)


def _year(value) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"(\d{4})", value)
    return int(match.group(1)) if match else None


def _sequence(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _narrators(value) -> list[str]:
    """Grimmory stores one narrator string.

    Semicolons are explicit separators. Commas separate several full names
    ("Jane Doe, John Smith"). A two-word "Last, First" credit stays one name.
    """
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    if ";" in text:
        return [part.strip() for part in text.split(";") if part.strip()]
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) == 2 and all(" " not in part for part in parts):
        return [text]
    return parts


def _file(raw: dict, *, playback_index: int | None = None) -> ABSFile:
    path = raw.get("filePath")
    exact = raw.get("fileSizeBytes")
    size_kb = raw.get("fileSizeKb")
    if type(exact) is int and exact >= 0:
        size, unit = exact, "byte"
    elif type(size_kb) is int and 0 <= size_kb <= 2_000_000_000:
        size, unit = size_kb * 1024, "kilobyte"
    else:
        raise ValueError("Incomplete file")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Incomplete file")
    extension = str(raw.get("extension") or "").lstrip(".").lower()
    book_type = raw.get("bookType")
    if not extension and isinstance(book_type, str):
        extension = EBOOK_TYPES.get(book_type, "m4b" if book_type == "AUDIOBOOK" else "")
    if book_type == "CBX" and extension not in {"cbz", "cbr", "cb7"}:
        extension = "cbz"
    return ABSFile(
        path=str(PurePosixPath(path)),
        size=size,
        format=extension or "unknown",
        playback_index=playback_index,
        size_unit=unit,
    )


def _isbn(metadata: dict) -> str | None:
    for key in ("isbn13", "isbn10"):
        value = metadata.get(key)
        if isinstance(value, str) and (cleaned := valid_isbn(value)):
            return cleaned
    return None


def _hardcover(metadata: dict) -> str | None:
    # hardcoverId is often a book slug. Edition matching needs the numeric book id.
    for key in ("hardcoverBookId", "hardcoverId"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip().isdigit():
            return value.strip()
    return None


def _authors(metadata: dict) -> tuple[list[str], bool]:
    kept, dropped = names(metadata.get("authors") or [])
    return [name.strip() for name in kept], dropped


def _book_folder(value: dict) -> str | None:
    primary = value.get("primaryFile") if isinstance(value.get("primaryFile"), dict) else {}
    path = primary.get("filePath")
    if not isinstance(path, str):
        return None
    return path if primary.get("folderBased") is True else str(PurePosixPath(path).parent)


def unreadable_book(value: dict, reason: str) -> ABSItem:
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    title = metadata.get("title") or value.get("title")
    if not isinstance(title, str) or not title.strip():
        title = folder_title(_book_folder(value)) or "Unread book"
    library_id = value.get("libraryId")
    return ABSItem(
        id=external_id(str(value["id"])),
        library_id=external_id(str(library_id)) if library_id is not None else "unknown",
        title=title.strip()[:600],
        authors=_authors(metadata)[0],
        narrators=_narrators(metadata.get("narrator")),
        path=_book_folder(value),
        invalid=True,
        unreadable=True,
        read_issues=[reason],
    )


def parse_book(value: dict, *, tracks: list | None = None) -> ABSItem:
    try:
        if value.get("isPhysical") is True:
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            title = metadata.get("title") or value.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError("Missing title")
            return ABSItem(
                id=external_id(str(value["id"])),
                library_id=external_id(str(value["libraryId"])),
                title=title.strip(),
                authors=[],
                narrators=[],
            )
        metadata = value.get("metadata")
        primary = value.get("primaryFile")
        if not isinstance(metadata, dict) or not isinstance(primary, dict):
            raise ValueError("Missing book metadata")
        raw_library_path = value.get("libraryPath")
        library_path = raw_library_path if isinstance(raw_library_path, dict) else {}
        extras = []
        for key in ("alternativeFormats", "supplementaryFiles"):
            rows = value.get(key) or []
            if not isinstance(rows, list):
                raise ValueError("Invalid file list")
            extras.extend(rows)
        if tracks:
            extras.extend(tracks)
        # A folder-based audiobook's primary path is the directory. Its tracks are the media.
        folder_based = primary.get("folderBased") is True
        seen, raw_files = set(), []
        for raw in [*([] if folder_based else [primary]), *extras]:
            if not isinstance(raw, dict) or not isinstance(raw.get("filePath"), str):
                raise ValueError("Invalid file")
            if raw["filePath"] in seen:
                continue
            seen.add(raw["filePath"])
            raw_files.append(raw)
        supplementary_paths = {
            raw["filePath"]
            for raw in (value.get("supplementaryFiles") or [])
            if isinstance(raw, dict) and isinstance(raw.get("filePath"), str)
        }
        audio_raw = [raw for raw in raw_files if raw.get("bookType") == "AUDIOBOOK"]
        audio_raw.sort(
            key=lambda raw: (
                raw["playbackIndex"] if type(raw.get("playbackIndex")) is int else 10**9,
                raw["filePath"],
            )
        )
        audio = [_file(raw, playback_index=index) for index, raw in enumerate(audio_raw, start=1)]
        ebook = [
            _file(raw)
            for raw in raw_files
            if raw.get("bookType") in EBOOK_TYPES and raw["filePath"] not in supplementary_paths
        ]
        duration = 0
        audio_meta = metadata.get("audiobookMetadata")
        if isinstance(audio_meta, dict) and type(audio_meta.get("durationSeconds")) is int:
            duration = audio_meta["durationSeconds"]
        if duration <= 0 and tracks:
            millis = sum(
                track.get("durationMs")
                for track in tracks
                if isinstance(track, dict) and type(track.get("durationMs")) is int
            )
            if millis > 0:
                duration = max(1, millis // 1000)
        full_audio = bool(audio and all(file.size > 0 for file in audio) and duration > 0)
        full_ebook = bool(ebook and all(file.size > 0 for file in ebook))
        ebook_supplementary = bool(
            not ebook
            and supplementary_paths
            or (audio and ebook and all(file.format == "pdf" for file in ebook))
        )
        issues = []
        authors, dropped = _authors(metadata)
        if dropped:
            issues.append("authors")
        identifiers = {}
        if isbn := _isbn(metadata):
            identifiers["isbn"] = isbn
        if isinstance(metadata.get("asin"), str) and metadata["asin"].strip():
            identifiers["asin"] = metadata["asin"].strip()
        if hardcover := _hardcover(metadata):
            identifiers["hardcover"] = hardcover
        series = []
        if isinstance(metadata.get("seriesName"), str) and metadata["seriesName"].strip():
            record = {"name": metadata["seriesName"].strip()}
            sequence = _sequence(metadata.get("seriesNumber"))
            if sequence:
                record["sequence"] = sequence
            series.append(record)
        primary_path = str(PurePosixPath(primary["filePath"]))
        folder = primary_path if folder_based else str(PurePosixPath(primary_path).parent)
        if isinstance(library_path.get("path"), str):
            backend_path(library_path["path"])
        backend_path(folder)
        title = metadata.get("title") or value.get("title")
        if not isinstance(title, str) or not title.strip():
            issues.append("title")
            title = folder_title(folder) or "Untitled book"
        language = metadata.get("language")
        language = catalog_language(language) if isinstance(language, str) else None
        if language and len(language) > 20:
            language = language[:20]
        for key, kind, issue in (
            ("narrator", str, "narrators"),
            ("language", str, "language"),
            ("description", str, "description"),
            ("abridged", bool, "abridged"),
            ("publishedDate", str, "year"),
        ):
            if metadata.get(key) is not None and not isinstance(metadata[key], kind):
                issues.append(issue)
        published = metadata.get("publishedDate")
        if isinstance(published, str) and published.strip() and _year(published) is None:
            issues.append("year")
        return ABSItem(
            id=external_id(str(value["id"])),
            library_id=external_id(str(value["libraryId"])),
            path=folder,
            library_files=[_file(raw) for raw in raw_files],
            series=series,
            cover_path=f"grimmory:{value['id']}",
            title=title.strip(),
            authors=authors,
            narrators=_narrators(metadata.get("narrator")) if audio else [],
            language=language,
            description=metadata.get("description")
            if isinstance(metadata.get("description"), str)
            else None,
            year=_year(metadata.get("publishedDate")),
            abridged=metadata.get("abridged") if type(metadata.get("abridged")) is bool else None,
            identifiers=identifiers,
            audio=audio,
            ebook=ebook,
            full_audio=full_audio,
            full_ebook=full_ebook and not ebook_supplementary,
            ebook_supplementary=ebook_supplementary,
            read_issues=issues,
            read_issue_values={
                issue: issue_preview(metadata[field])
                for issue, field in (
                    ("year", "publishedDate"),
                    ("language", "language"),
                    ("abridged", "abridged"),
                )
                if issue in issues and metadata.get(field) is not None
            },
        )
    except (KeyError, TypeError, ValueError, AdapterError) as error:
        raise AdapterError(
            FailureKind.PARSER, "Grimmory book metadata or file evidence is incomplete."
        ) from error


def _summary(book: dict) -> dict:
    metadata = book.get("metadata") if isinstance(book.get("metadata"), dict) else {}
    primary = book.get("primaryFile") if isinstance(book.get("primaryFile"), dict) else {}
    marker = json.dumps(
        {
            "title": metadata.get("title"),
            "authors": metadata.get("authors"),
            "narrator": metadata.get("narrator"),
            "language": metadata.get("language"),
            "published": metadata.get("publishedDate"),
            "series": metadata.get("seriesName"),
            "sequence": metadata.get("seriesNumber"),
            "duration": (metadata.get("audiobookMetadata") or {}).get("durationSeconds")
            if isinstance(metadata.get("audiobookMetadata"), dict)
            else None,
            "isbn": metadata.get("isbn13") or metadata.get("isbn10"),
            "asin": metadata.get("asin"),
            "hardcover": metadata.get("hardcoverBookId") or metadata.get("hardcoverId"),
            "path": primary.get("filePath"),
            "size": primary.get("fileSizeKb"),
            "added": book.get("addedOn"),
            "cover": metadata.get("coverUpdatedOn"),
            "physical": book.get("isPhysical"),
        },
        sort_keys=True,
        default=str,
    )
    return {
        "id": external_id(str(book["id"])),
        "updatedAt": hashlib.sha256(marker.encode()).hexdigest(),
        "isMissing": False,
        "isInvalid": False,
    }


class Grimmory(JsonEndpoint):
    kind = "grimmory"
    page_size = 20

    def __init__(self, base_url: str, credentials, *, transport=None):
        token = credentials if isinstance(credentials, str) else None
        self._credentials = None if isinstance(credentials, str) else credentials
        self._catalog_db = None
        self._catalog_file = None
        self._catalog_lock = asyncio.Lock()
        self._catalog_counts = {}
        self._catalog_cursors = {}
        self._reauth = False
        super().__init__(base_url, token, transport=transport)
        self.client.timeout = httpx.Timeout(120, connect=10)

    def _account_error(self, error: AdapterError) -> AdapterError:
        if not self._credentials:
            return error
        if error.kind == FailureKind.AUTHENTICATION:
            return AdapterError(
                FailureKind.AUTHENTICATION,
                "Grimmory rejected the username or password. Update the connection.",
            )
        if error.kind == FailureKind.PERMISSION:
            return AdapterError(
                FailureKind.PERMISSION,
                "This Grimmory account cannot access the requested library or operation.",
            )
        return error

    async def request(self, method, path, **kwargs):
        try:
            return await super().request(method, path, **kwargs)
        except AdapterError as error:
            if (
                error.kind != FailureKind.AUTHENTICATION
                or path == "api/v1/auth/login"
                or not self._credentials
                or self._reauth
            ):
                replacement = self._account_error(error)
                if replacement is not error:
                    raise replacement from error
                raise
            self._reauth = True
            try:
                await self._login()
                return await super().request(method, path, **kwargs)
            except AdapterError as retry_error:
                replacement = self._account_error(retry_error)
                if replacement is not retry_error:
                    raise replacement from retry_error
                raise
            finally:
                self._reauth = False

    async def __aenter__(self):
        await super().__aenter__()
        if self._credentials is not None:
            await self._login()
        return self

    async def _login(self):
        username = self._credentials.get("username")
        password = self._credentials.get("password")
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or not username
            or not password
        ):
            raise AdapterError(
                FailureKind.AUTHENTICATION, "Enter the Grimmory username and password."
            )
        self.client.headers.pop("Authorization", None)
        tokens = await self.request(
            "POST", "api/v1/auth/login", json={"username": username, "password": password}
        )
        access = tokens.get("accessToken")
        if not isinstance(access, str) or not access:
            raise AdapterError(FailureKind.PARSER, "Grimmory did not return an access token.")
        self.client.headers["Authorization"] = f"Bearer {access}"

    async def server_version(self) -> str:
        response = await self.request("GET", "api/v1/version")
        current = response.get("current")
        if not isinstance(current, str) or not current.strip():
            raise AdapterError(FailureKind.PARSER, "Grimmory did not identify its server version.")
        return current.strip().removeprefix("v")

    async def authorize(self) -> tuple[Capabilities, str]:
        user = await self.request("GET", "api/v1/users/me")
        permissions = user.get("permissions")
        if (
            not isinstance(user.get("id"), int)
            or not isinstance(user.get("username"), str)
            or not isinstance(permissions, dict)
        ):
            raise AdapterError(FailureKind.PARSER, "Grimmory did not return account capabilities.")
        version = await self.server_version()
        manage = bool(permissions.get("admin") or permissions.get("canManageLibrary"))
        edit = bool(permissions.get("admin") or permissions.get("canEditMetadata"))
        operations = {"inventory", "item", "deep_link"}
        if manage:
            operations.add("scan")
        if edit:
            operations.add("metadata")
        libraries = user.get("assignedLibraries") or []
        if not isinstance(libraries, list):
            raise AdapterError(FailureKind.PARSER, "Grimmory returned invalid library access.")
        scope = hashlib.sha256(
            json.dumps(
                {
                    "id": user["id"],
                    "permissions": permissions,
                    "libraries": sorted(
                        library.get("id") for library in libraries if isinstance(library, dict)
                    ),
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return Capabilities(
            version=version,
            operations=operations,
            limitations=[]
            if "scan" in operations
            else ["Library detection relies on Grimmory's folder watch."],
        ), scope

    async def libraries(self) -> list[dict]:
        values = await self.request("GET", "api/v1/libraries", allow_list=True)
        if not isinstance(values, list):
            raise AdapterError(FailureKind.PARSER, "Grimmory returned an invalid library list.")
        result, seen = [], set()
        for library in values:
            if not isinstance(library, dict) or not isinstance(library.get("name"), str):
                raise AdapterError(FailureKind.PARSER, "Grimmory returned an invalid library.")
            if type(library.get("id")) is not int:
                raise AdapterError(FailureKind.PARSER, "Grimmory returned an invalid library.")
            key = external_id(str(library["id"]))
            if key in seen:
                raise AdapterError(FailureKind.PARSER, "Grimmory returned duplicate libraries.")
            seen.add(key)
            result.append({"id": key, "name": library["name"][:200]})
        return result

    async def import_configuration(self, library_id: str) -> GrimmoryImportConfiguration:
        response = await self.request("GET", f"api/v1/libraries/{external_id(library_id)}")
        try:
            if str(response["id"]) != library_id:
                raise ValueError("Unexpected library")
            paths = response["paths"]
            if not isinstance(paths, list) or not paths:
                raise ValueError("Missing library roots")
            roots = [backend_path(path["path"]) for path in paths]
            allowed = response.get("allowedFormats") or []
            if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
                raise ValueError("Invalid formats")
            audio_allowed = not allowed or "AUDIOBOOK" in allowed
            ebook_allowed = not allowed or any(item in EBOOK_TYPES for item in allowed)
            mode = response["organizationMode"]
            source = response["metadataSource"]
            watch = response["watch"]
            if type(watch) is not bool or not isinstance(mode, str) or not isinstance(source, str):
                raise ValueError("Invalid library settings")
            return GrimmoryImportConfiguration(
                library_id=library_id,
                folders=roots,
                audiobooks_only=audio_allowed and not ebook_allowed,
                audio_allowed=audio_allowed,
                watcher_enabled=watch,
                organization_mode=mode,
                metadata_source=source,
                allowed_formats=allowed,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise AdapterError(
                FailureKind.PARSER,
                "Grimmory library import settings are incomplete or unsupported.",
            ) from error

    async def path_exists(self, root: str, name: str) -> bool:
        backend_path(root)
        if not re.fullmatch(r"book-search-check-[a-f0-9]{32}", name):
            raise ValueError("Only generated mapping challenge names are permitted")
        try:
            values = await self.request(
                "GET", "api/v1/path", params={"path": root}, allow_list=True
            )
        except AdapterError as error:
            if error.kind == FailureKind.PERMISSION:
                raise AdapterError(
                    FailureKind.PERMISSION,
                    "Grimmory library management permission is required "
                    "for its folder-mapping check. "
                    "Inventory-only connections can still sync.",
                ) from error
            raise
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise AdapterError(FailureKind.PARSER, "Grimmory did not return a path check result.")
        return any(value == name or value.rstrip("/").endswith("/" + name) for value in values)

    async def __aexit__(self, *args):
        try:
            await self._close_catalog()
        finally:
            await super().__aexit__(*args)

    async def _close_catalog(self):
        self._catalog_counts.clear()
        self._catalog_cursors.clear()
        if self._catalog_db is not None:
            await asyncio.to_thread(self._catalog_db.close)
            self._catalog_db = None
        if self._catalog_file is not None:
            self._catalog_file.cleanup()
            self._catalog_file = None

    async def refresh_snapshot(self) -> None:
        """Re-read all libraries into a bounded disk spool for verification."""
        async with self._catalog_lock:
            await self._close_catalog()
            await self._download_catalog()

    async def _download_catalog(self):
        self._catalog_file = tempfile.TemporaryDirectory(prefix="dewarr-inventory-")
        connection = sqlite3.connect(
            self._catalog_file.name + "/catalog.sqlite", check_same_thread=False
        )
        try:
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute(
                "CREATE TABLE books (id TEXT PRIMARY KEY, library TEXT NOT NULL, "
                "value TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX books_library_id ON books(library, length(id), id)")
            page, count, total = 0, 0, None
            while True:
                response = await self.request(
                    "GET",
                    "api/v1/books/page",
                    params={"page": page, "size": self.page_size},
                    timeout_seconds=120,
                    max_bytes=CATALOG_PAGE_BYTES,
                    response_label="Grimmory inventory page",
                )
                content, meta = response.get("content"), response.get("page")
                if not isinstance(content, list) or not isinstance(meta, dict):
                    raise AdapterError(
                        FailureKind.PARSER, "Grimmory returned invalid pagination data."
                    )
                reported, pages = meta.get("totalElements"), meta.get("totalPages")
                if type(reported) is not int or reported < 0 or type(pages) is not int or pages < 0:
                    raise AdapterError(
                        FailureKind.PARSER, "Grimmory returned invalid pagination data."
                    )
                if total is None:
                    total = reported
                if total != reported or len(content) != min(self.page_size, total - count):
                    raise AdapterError(
                        FailureKind.UNCERTAIN, "Library size changed during sync. Try again."
                    )
                if pages != (total + self.page_size - 1) // self.page_size and total:
                    raise AdapterError(
                        FailureKind.UNCERTAIN, "Grimmory returned inconsistent page counts."
                    )
                rows = []
                for book in content:
                    if not isinstance(book, dict) or type(book.get("id")) is not int:
                        raise AdapterError(
                            FailureKind.PARSER, "Grimmory returned an invalid book list."
                        )
                    rows.append(
                        (external_id(str(book["id"])), str(book.get("libraryId")), json.dumps(book))
                    )

                def write_page(rows=rows):
                    connection.executemany("INSERT INTO books VALUES (?, ?, ?)", rows)
                    connection.commit()

                try:
                    await asyncio.to_thread(write_page)
                except sqlite3.IntegrityError as error:
                    raise AdapterError(
                        FailureKind.UNCERTAIN, "Library pagination repeated an item."
                    ) from error
                count += len(content)
                if count == total:
                    break
                if not content:
                    raise AdapterError(
                        FailureKind.UNCERTAIN, "Library pagination did not make progress."
                    )
                page += 1
            self._catalog_counts = dict(
                await asyncio.to_thread(
                    lambda: connection.execute(
                        "SELECT library, count(*) FROM books GROUP BY library"
                    ).fetchall()
                )
            )
            self._catalog_db = connection
        except BaseException:
            await asyncio.to_thread(connection.close)
            self._catalog_file.cleanup()
            self._catalog_file = None
            raise

    async def page(self, library_id: str, page: int) -> tuple[list[dict], int]:
        library_id = external_id(library_id)
        async with self._catalog_lock:
            if self._catalog_db is None:
                await self._download_catalog()

            def read_page():
                previous = self._catalog_cursors.get(library_id)
                if previous and page == previous[0] + 1:
                    records = self._catalog_db.execute(
                        "SELECT id, value FROM books WHERE library=? AND (length(id), id) > (?, ?) "
                        "ORDER BY length(id), id LIMIT ?",
                        (library_id, len(previous[1]), previous[1], self.page_size),
                    ).fetchall()
                else:
                    records = self._catalog_db.execute(
                        "SELECT id, value FROM books WHERE library=? "
                        "ORDER BY length(id), id LIMIT ? OFFSET ?",
                        (library_id, self.page_size, page * self.page_size),
                    ).fetchall()
                if records:
                    self._catalog_cursors[library_id] = (page, records[-1][0])
                return [_summary(json.loads(row[1])) for row in records], self._catalog_counts.get(
                    library_id, 0
                )

            return await asyncio.to_thread(read_page)

    async def _tracks(self, book: dict) -> list:
        primary = book.get("primaryFile") if isinstance(book.get("primaryFile"), dict) else {}
        if primary.get("bookType") != "AUDIOBOOK" or not primary.get("folderBased"):
            return []
        folder = primary.get("filePath")
        if not isinstance(folder, str) or not folder.startswith("/"):
            raise AdapterError(FailureKind.PARSER, "Grimmory returned an invalid audiobook folder.")
        info = await self.request("GET", f"api/v1/audiobooks/{external_id(str(book['id']))}/info")
        tracks = info.get("tracks")
        if not isinstance(tracks, list):
            raise AdapterError(
                FailureKind.PARSER, "Grimmory returned an invalid audiobook track list."
            )
        files = []
        for track in tracks:
            if not isinstance(track, dict):
                raise AdapterError(
                    FailureKind.PARSER, "Grimmory returned an invalid audiobook track."
                )
            name = track.get("fileName")
            size = track.get("fileSizeBytes")
            if not isinstance(name, str) or "/" in name or name in {"", ".", ".."}:
                raise AdapterError(
                    FailureKind.PARSER, "Grimmory returned an invalid audiobook track."
                )
            if type(size) is not int or size < 0:
                raise AdapterError(
                    FailureKind.PARSER, "Grimmory returned an invalid audiobook track."
                )
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            record = {
                "filePath": str(PurePosixPath(folder) / name),
                "fileSizeBytes": size,
                "extension": extension,
                "bookType": "AUDIOBOOK",
                "folderBased": False,
                "playbackIndex": track.get("index") if type(track.get("index")) is int else None,
                "durationMs": track.get("durationMs"),
            }
            files.append(record)
        return files

    async def _one_book(self, book: dict) -> ABSItem:
        try:
            tracks = await self._tracks(book)
            item = parse_book(book, tracks=tracks)
        except AdapterError as error:
            # A malformed row stays in the census. Auth and transport failures still stop the sync.
            if error.kind != FailureKind.PARSER:
                raise
            item = unreadable_book(book, parse_reason(error))
        if item.read_issues:
            logger.warning(
                "Grimmory book %s %s (%s)",
                item.id,
                "could not be read" if item.unreadable else "was read without some fields",
                ", ".join(item.read_issues),
            )
        return item

    async def _book_details(self, ids: list[str]) -> list[dict]:
        return [book async for book in read_batches(ids, self._book_detail_batch, size=20)]

    async def _book_detail_batch(self, ids: list[str]) -> list[dict]:
        """Read full books. The paged list omits folder grouping and edition ids."""
        numeric = [external_id(item_id) for item_id in ids]
        try:
            values = await self.request(
                "GET",
                "api/v1/books/batch",
                params={"ids": numeric},
                allow_list=True,
            )
        except AdapterError as error:
            if error.kind != FailureKind.NOT_FOUND:
                raise
            values = [await self.request("GET", f"api/v1/books/{item_id}") for item_id in numeric]
        if not isinstance(values, list) or not all(isinstance(book, dict) for book in values):
            raise AdapterError(FailureKind.PARSER, "Grimmory returned an invalid book list.")
        return values

    async def expanded(self, ids: list[str]) -> list[ABSItem]:
        if not ids:
            return []
        items = [await self._one_book(book) for book in await self._book_details(ids)]
        if {item.id for item in items} != set(ids):
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library item details did not match the requested page."
            )
        return items

    async def item(self, item_id: str) -> ABSItem:
        book = await self.request("GET", f"api/v1/books/{external_id(item_id)}")
        return await self._one_book(book)

    async def apply_catalog_metadata(
        self, book_id: str, facts: dict, *, observed_year: int | None = None
    ) -> None:
        """Write Dewarr's catalog fields onto a book Grimmory has already detected.

        Refresh and folder watch index embedded file metadata. The sidecar file is
        not applied until this explicit update, so confirmation would otherwise
        compare the catalog with whatever was inside the file. A year is written
        only when Grimmory's date is missing or from another year, because the
        catalog has no month or day to replace one Grimmory already read.
        """
        medium = facts.get("medium")
        year = facts.get("recording_year") if medium == "audio" else facts.get("edition_year")
        metadata: dict = {}
        title = facts.get("title")
        if isinstance(title, str) and title.strip():
            metadata["title"] = title.strip()
        authors = [
            name.strip()
            for name in facts.get("authors") or []
            if isinstance(name, str) and name.strip()
        ]
        if authors:
            metadata["authors"] = authors
        if isinstance(facts.get("subtitle"), str) and facts["subtitle"].strip():
            metadata["subtitle"] = facts["subtitle"].strip()
        if isinstance(facts.get("publisher"), str) and facts["publisher"].strip():
            metadata["publisher"] = facts["publisher"].strip()
        if isinstance(year, int) and year != observed_year:
            metadata["publishedDate"] = f"{year:04d}-01-01"
        if isinstance(facts.get("language"), str) and facts["language"].strip():
            metadata["language"] = facts["language"].strip()
        isbn_value = facts.get("isbn")
        if isinstance(isbn_value, str) and (isbn := valid_isbn(isbn_value)):
            metadata["isbn13" if len(isbn) == 13 else "isbn10"] = isbn
        if isinstance(facts.get("asin"), str) and re.fullmatch(r"[A-Z0-9]{10}", facts["asin"]):
            metadata["asin"] = facts["asin"]
        if isinstance(facts.get("series"), str) and facts["series"].strip():
            metadata["seriesName"] = facts["series"].strip()
            sequence = facts.get("sequence")
            if sequence is not None and str(sequence).strip():
                try:
                    metadata["seriesNumber"] = float(sequence)
                except (TypeError, ValueError):
                    pass
        if medium == "audio":
            names = [
                name.strip()
                for name in facts.get("narrators") or []
                if isinstance(name, str) and name.strip()
            ]
            if names:
                metadata["narrator"] = ", ".join(names)
        if type(facts.get("abridged")) is bool:
            metadata["abridged"] = facts["abridged"]
        if not metadata:
            return
        await self.request(
            "PUT",
            f"api/v1/books/{external_id(book_id)}/metadata",
            params={"mergeCategories": "false", "replaceMode": "REPLACE_WHEN_PROVIDED"},
            json={"metadata": metadata},
        )

    async def scan(self, library_id: str) -> None:
        await self.request("PUT", f"api/v1/libraries/{external_id(library_id)}/refresh", empty=True)

    async def cover(self, book_id: str) -> tuple[bytes, str]:
        item_id = external_id(book_id)
        try:
            async with (
                asyncio.timeout(15),
                self.client.stream("GET", f"api/v1/media/book/{item_id}/cover") as response,
            ):
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if response.status_code != 200 or kind not in {
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    "image/avif",
                    "image/gif",
                }:
                    raise AdapterError(FailureKind.NOT_FOUND, "Cover unavailable")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise AdapterError(FailureKind.PARSER, "Cover unavailable")
                return bytes(content), kind
        except (httpx.HTTPError, TimeoutError) as error:
            raise AdapterError(FailureKind.UNAVAILABLE, "Cover unavailable") from error
