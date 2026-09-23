import hashlib
import json
import logging
import re
from pathlib import PurePosixPath
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.adapters.contracts import AdapterError, Capabilities, FailureKind
from app.adapters.http import JsonEndpoint
from app.domain.catalog_language import catalog_language

logger = logging.getLogger(__name__)


def external_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise AdapterError(
            FailureKind.PARSER, "Audiobookshelf returned an invalid item identifier."
        )
    return value


class ABSFile(BaseModel):
    path: str
    size: int = Field(ge=0)
    format: str
    inode: str | None = None
    modified: float | None = None
    playback_index: int | None = Field(default=None, ge=1)
    # Grimmory reports kilobytes. size is then fileSizeKb * 1024, not an exact byte count.
    size_unit: Literal["byte", "kilobyte"] = "byte"


class ABSItem(BaseModel):
    id: str
    library_id: str
    old_id: str | None = None
    title: str = Field(min_length=1, max_length=600)
    authors: list[str]
    narrators: list[str]
    language: str | None = Field(default=None, max_length=20)
    description: str | None = None
    year: int | None = None
    abridged: bool | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    audio: list[ABSFile] = Field(default_factory=list)
    ebook: list[ABSFile] = Field(default_factory=list)
    missing: bool = False
    invalid: bool = False
    full_audio: bool = False
    full_ebook: bool = False
    ebook_supplementary: bool = False
    # One unreadable library row must not abort the rest of the sync.
    unreadable: bool = False
    path: str | None = None
    library_files: list[ABSFile] = Field(default_factory=list)
    series: list[dict] = Field(default_factory=list)
    cover_path: str | None = None


class ABSImportConfiguration(BaseModel):
    library_id: str
    folders: list[str]
    audiobooks_only: bool
    watcher_enabled: bool
    metadata_precedence: list[str]


_UNCONFINED = "Backend library path must be a confined absolute path"

# ABS fills in missing library settings only when a library is edited, so older libraries
# can omit keys. ABS reads a missing audiobooksOnly or disableWatcher as false and a missing
# metadataPrecedence as this order.
_DEFAULT_METADATA_PRECEDENCE = [
    "folderStructure",
    "audioMetatags",
    "nfoFile",
    "txtFiles",
    "opfFile",
    "absMetadata",
]


def _confined(parts: list[str], *, minimum: int) -> None:
    if len(parts) < minimum or any(part in {".", "..", ""} for part in parts):
        raise ValueError(_UNCONFINED)


def metadata_patch(title, authors, narrators):
    """Narrators are omitted when the new list is empty so an existing list is left alone."""
    metadata = {
        "title": title,
        "authors": [{"name": name} for name in authors if name.strip()],
    }
    if narrators:
        metadata["narrators"] = [{"name": name} for name in narrators if name.strip()]
    return {"metadata": metadata}


def backend_path(value):
    """Library root as Audiobookshelf stores it.

    Drive roots use forward slashes, including a drive itself (``D:/``). UNC roots
    keep the leading backslashes Audiobookshelf saved after ``Path.resolve``
    (``\\\\host/share/Books``). A leading ``//`` is the same root; collapsing it
    to one slash, or sending it back as ``//``, misses the stored folder.
    """
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        raise ValueError(_UNCONFINED)
    if value.startswith("\\\\") or value.startswith("//"):
        parts = value[2:].replace("\\", "/").split("/")
        _confined(parts, minimum=2)
        return "\\\\" + "/".join(parts)
    windows = value.replace("\\", "/")
    if re.fullmatch(r"[A-Za-z]:/", windows):
        return windows
    if re.fullmatch(r"[A-Za-z]:(?:/.*)?", windows):
        _confined(windows.split("/")[1:], minimum=1)
        return windows
    if (
        not value.startswith("/")
        or value == "/"
        or "\\" in value
        or any(part in {".", "..", ""} for part in value[1:].split("/"))
    ):
        raise ValueError(_UNCONFINED)
    return str(PurePosixPath(value))


def _language(value):
    """ABS keeps whatever the tags or metadata file said, such as "English (United States)"."""
    if not isinstance(value, str):
        return None
    if len(value) > 20:
        value = catalog_language(value)
    return value if value is not None and len(value) <= 20 else None


def unreadable_item(value: dict) -> ABSItem:
    media = value.get("media") if isinstance(value.get("media"), dict) else {}
    metadata = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "Unread item"
    return ABSItem(
        id=external_id(value.get("id")),
        library_id=external_id(value.get("libraryId")),
        title=title.strip()[:600],
        authors=[],
        narrators=[],
        invalid=True,
        unreadable=True,
    )


def _parse_reason(error: AdapterError) -> str:
    """Field names only. Values can hold private titles and file paths."""
    cause = error.__cause__
    if isinstance(cause, ValidationError):
        return ", ".join(".".join(map(str, detail["loc"])) for detail in cause.errors())
    if isinstance(cause, KeyError):
        return f"missing {cause.args[0]!r}" if cause.args else "missing field"
    if isinstance(cause, ValueError):
        return str(cause)
    return type(cause).__name__ if cause else str(error)


def readable_item(value: Any) -> ABSItem:
    if not isinstance(value, dict):
        raise AdapterError(FailureKind.PARSER, "Audiobookshelf returned an invalid item list.")
    try:
        return parse_item(value)
    except AdapterError as error:
        # A malformed row stays in the census so one book cannot hold the whole library.
        item = unreadable_item(value)
        logger.warning(
            "Audiobookshelf item %s could not be read (%s)", item.id, _parse_reason(error)
        )
        return item


def parse_item(value: dict) -> ABSItem:
    try:
        media = value["media"]
        metadata = media["metadata"]
        if value.get("mediaType") != "book" or not isinstance(metadata, dict):
            raise ValueError("Unexpected media type")
        files = value["libraryFiles"]
        if not isinstance(files, list):
            raise ValueError("Missing file evidence")
        indexed = {file["metadata"]["path"]: file for file in files}

        def file_evidence(file: dict) -> ABSFile:
            source = file["metadata"]
            if source["path"] not in indexed:
                raise ValueError("Unlisted media file")
            return ABSFile(
                path=source["path"],
                size=source["size"],
                format=(file.get("ebookFormat") or source.get("ext") or "").lstrip(".").lower(),
                inode=str(file["ino"]) if file.get("ino") is not None else None,
                modified=source.get("mtimeMs"),
                playback_index=file.get("index")
                if isinstance(file.get("index"), int) and file["index"] > 0
                else None,
            )

        all_audio = media.get("audioFiles", [])
        if not isinstance(all_audio, list):
            raise ValueError("Invalid audio file list")
        active = [file for file in all_audio if not file.get("exclude")]
        audio = [file_evidence(file) for file in active]
        primary = media.get("ebookFile")
        ebook = [file_evidence(primary)] if primary else []
        primary_supplementary = primary and indexed[primary["metadata"]["path"]].get(
            "isSupplementary", False
        )
        # A PDF next to an audiobook is conservatively treated as supporting material.
        ebook_supplementary = bool(
            primary_supplementary or (audio and ebook and ebook[0].format == "pdf")
        )
        full_ebook = bool(ebook and ebook[0].size > 0 and not ebook_supplementary)
        full_audio = bool(
            audio
            and all(file.size > 0 for file in audio)
            and all(
                not file.get("isInvalid")
                and not file.get("error")
                and (file.get("duration") or 0) > 0
                for file in active
            )
        )
        author_records = metadata.get("authors", [])
        if not isinstance(author_records, list):
            raise ValueError("Invalid authors")
        authors = [record["name"] for record in author_records]
        narrators = metadata.get("narrators") or []
        if not all(isinstance(name, str) for name in authors + narrators):
            raise ValueError("Invalid contributor names")
        year = str(metadata.get("publishedYear") or "")
        return ABSItem(
            id=external_id(value["id"]),
            library_id=external_id(value["libraryId"]),
            path=value.get("path"),
            library_files=[file_evidence(file) for file in files],
            series=metadata.get("series") or [],
            cover_path=media.get("coverPath"),
            old_id=external_id(value["oldLibraryItemId"])
            if value.get("oldLibraryItemId")
            else None,
            title=metadata["title"],
            authors=authors,
            narrators=narrators,
            language=_language(metadata.get("language")),
            description=metadata.get("descriptionPlain"),
            year=int(year) if re.fullmatch(r"\d{4}", year) else None,
            abridged=metadata.get("abridged"),
            identifiers={key: str(metadata[key]) for key in ("isbn", "asin") if metadata.get(key)},
            audio=audio,
            ebook=ebook,
            missing=bool(value.get("isMissing")),
            invalid=bool(value.get("isInvalid")),
            full_audio=full_audio,
            full_ebook=full_ebook,
            ebook_supplementary=ebook_supplementary,
        )
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as error:
        raise AdapterError(
            FailureKind.PARSER, "Audiobookshelf item metadata or file evidence is incomplete."
        ) from error


class Audiobookshelf(JsonEndpoint):
    page_size = 100

    async def import_configuration(self, library_id: str) -> ABSImportConfiguration:
        response = await self.request("GET", f"api/libraries/{external_id(library_id)}")
        try:
            if response["id"] != library_id or response["mediaType"] != "book":
                raise ValueError("Unexpected library identity or type")
            folders = response["folders"]
            if not isinstance(folders, list) or not folders:
                raise ValueError("Missing library roots")
            roots = []
            for folder in folders:
                path = folder.get("fullPath") or folder.get("path")
                try:
                    roots.append(backend_path(path))
                except ValueError as error:
                    raise AdapterError(
                        FailureKind.UNSUPPORTED,
                        f"Dewarr cannot use the Audiobookshelf folder path {str(path)[:200]!r}.",
                    ) from error
            settings = response.get("settings") or {}

            def setting(key, default):
                value = settings.get(key)
                return default if value is None else value

            audio_only = setting("audiobooksOnly", False)
            disabled = setting("disableWatcher", False)
            precedence = setting("metadataPrecedence", list(_DEFAULT_METADATA_PRECEDENCE))
            if (
                type(audio_only) is not bool
                or type(disabled) is not bool
                or not isinstance(precedence, list)
                or not all(isinstance(value, str) for value in precedence)
                or len(set(precedence)) != len(precedence)
            ):
                raise ValueError("Invalid import settings")
            return ABSImportConfiguration(
                library_id=library_id,
                folders=roots,
                audiobooks_only=audio_only,
                watcher_enabled=not disabled,
                metadata_precedence=precedence,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise AdapterError(
                FailureKind.PARSER,
                "Audiobookshelf library import settings are incomplete or unsupported.",
            ) from error

    async def path_exists(self, root: str, name: str) -> bool:
        # This read-only upstream POST also requires ABS upload permission.
        # The lookup is an exact match on the stored folder path.
        root = backend_path(root)
        if not re.fullmatch(r"book-search-check-[a-f0-9]{32}", name):
            raise ValueError("Only generated mapping challenge names are permitted")
        try:
            response = await self.request(
                "POST", "api/filesystem/pathexists", json={"folderPath": root, "directory": name}
            )
        except AdapterError as error:
            if error.kind == FailureKind.PERMISSION:
                raise AdapterError(
                    FailureKind.PERMISSION,
                    "ABS upload permission is required for its folder-mapping check. "
                    "Inventory-only connections can still sync.",
                ) from error
            raise
        if type(response.get("exists")) is not bool:
            raise AdapterError(
                FailureKind.PARSER, "Audiobookshelf did not return a path check result."
            )
        return response["exists"]

    async def server_version(self) -> str:
        response = await self.request("GET", "status")
        if response.get("app") != "audiobookshelf" or not isinstance(
            response.get("serverVersion"), str
        ):
            raise AdapterError(
                FailureKind.PARSER, "Audiobookshelf did not identify its server version."
            )
        return response["serverVersion"]

    async def authorize(self) -> tuple[Capabilities, str]:
        response = await self.request("POST", "api/authorize", json={})
        user = response.get("user")
        if (
            not isinstance(user, dict)
            or not isinstance(user.get("id"), str)
            or not isinstance(user.get("permissions", {}), dict)
        ):
            raise AdapterError(
                FailureKind.PARSER, "Audiobookshelf did not return account capabilities."
            )
        operations = {"inventory", "item", "deep_link"}
        if user.get("type") in {"root", "admin"}:
            operations.add("scan")
        scope = hashlib.sha256(
            json.dumps(
                {
                    "id": user["id"],
                    "type": user.get("type"),
                    "permissions": user.get("permissions", {}),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        settings = response.get("serverSettings") or {}
        if not isinstance(settings, dict):
            raise AdapterError(FailureKind.PARSER, "Audiobookshelf returned invalid capabilities.")
        version = settings.get("version") or response.get("serverVersion")
        if version is not None and not isinstance(version, str):
            raise AdapterError(FailureKind.PARSER, "Audiobookshelf returned an invalid version.")
        return Capabilities(
            version=version,
            operations=operations,
            limitations=[]
            if "scan" in operations
            else ["Library detection relies on Audiobookshelf's watcher."],
        ), scope

    async def libraries(self) -> list[dict]:
        response = await self.request("GET", "api/libraries")
        values = response.get("libraries")
        if not isinstance(values, list):
            raise AdapterError(
                FailureKind.PARSER, "Audiobookshelf returned an invalid library list."
            )
        result = []
        seen = set()
        for library in values:
            if not isinstance(library, dict) or not isinstance(library.get("name"), str):
                raise AdapterError(
                    FailureKind.PARSER, "Audiobookshelf returned an invalid library."
                )
            key = external_id(library.get("id"))
            if key in seen:
                raise AdapterError(
                    FailureKind.PARSER, "Audiobookshelf returned duplicate libraries."
                )
            seen.add(key)
            if library.get("mediaType") == "book":
                result.append({"id": key, "name": library["name"][:200]})
        return result

    async def page(self, library_id: str, page: int) -> tuple[list[dict], int]:
        response = await self.request(
            "GET",
            f"api/libraries/{external_id(library_id)}/items",
            params={
                "page": page,
                "limit": self.page_size,
                "minified": 1,
                "sort": "addedAt",
                "desc": 0,
            },
        )
        results, total = response.get("results"), response.get("total")
        if not isinstance(results, list) or type(total) is not int or total < 0 or total > 100000:
            raise AdapterError(
                FailureKind.PARSER, "Audiobookshelf returned invalid pagination data."
            )
        expected = min(self.page_size, max(0, total - page * self.page_size))
        if len(results) != expected:
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library contents changed during sync. Try again."
            )
        for item in results:
            if not isinstance(item, dict):
                raise AdapterError(
                    FailureKind.PARSER, "Audiobookshelf returned an invalid item list."
                )
            external_id(item.get("id"))
        return results, total

    async def expanded(self, ids: list[str]) -> list[ABSItem]:
        response = await self.request(
            "POST",
            "api/items/batch/get",
            json={
                "libraryItemIds": [external_id(value) for value in ids],
            },
        )
        values = response.get("libraryItems")
        if not isinstance(values, list) or len(values) != len(ids):
            raise AdapterError(FailureKind.UNCERTAIN, "Library item details changed during sync.")
        items = [readable_item(value) for value in values]
        if {item.id for item in items} != set(ids):
            raise AdapterError(
                FailureKind.UNCERTAIN, "Library item details did not match the requested page."
            )
        return items

    async def item(self, item_id: str) -> ABSItem:
        return readable_item(
            await self.request("GET", f"api/items/{external_id(item_id)}", params={"expanded": 1})
        )

    async def scan(self, library_id: str) -> None:
        await self.request("POST", f"api/libraries/{external_id(library_id)}/scan", empty=True)

    async def update_item(self, item_id: str, *, title: str, authors: list[str], narrators):
        """Write metadata only. Audio files are not renamed or retagged."""
        await self.request(
            "PATCH",
            f"api/items/{external_id(item_id)}/media",
            json=metadata_patch(title, authors, narrators),
        )

    async def update_cover(self, item_id: str, content: bytes) -> None:
        item = external_id(item_id)
        try:
            response = await self.client.post(
                f"api/items/{item}/cover",
                files={"cover": ("cover.jpg", content, "image/jpeg")},
            )
        except httpx.HTTPError as error:
            raise AdapterError(
                FailureKind.ROUTE, "The server could not be reached. Check its URL and network."
            ) from error
        if not 200 <= response.status_code < 300:
            raise AdapterError(FailureKind.UNAVAILABLE, "The server could not update the cover.")
