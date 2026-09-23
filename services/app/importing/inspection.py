"""Inspect bytes and propose file groups; catalog identity remains a separate decision."""

import json
import math
import os
import re
import stat
import subprocess
import sys
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

from defusedxml import ElementTree
from pydantic import Field

from app.domain.catalog_titles import parse_title_labels
from app.importing.book_containers import check_zip_directory
from app.importing.filesystem import (
    InspectionError,
    beneath,
    digest,
    directory,
    enumerate_files,
    identity,
    relative_parts,
    source_scope,
)
from app.importing.naming import PlannedSourceFile, StrictModel, fingerprint
from app.importing.probe import probe_output


class InspectedFile(StrictModel):
    path: str
    extension: str
    state: str
    medium: str | None
    identity: dict[str, int]
    sha256: str
    reason: str | None = None
    technical: dict | None = None
    metadata: dict | None = None


class InspectedGroup(StrictModel):
    key: str
    medium: str
    title: str | None
    authors: list[str]
    narrators: list[str]
    files: list[PlannedSourceFile]
    identity: str
    full_content: str
    same_edition: bool = False


class InspectionSnapshot(StrictModel):
    schema_version: int
    source_path: str
    relative_path: str
    source_kind: Literal["directory", "file"] = "directory"
    directory_identity: dict[str, int]
    files: list[InspectedFile]
    groups: list[InspectedGroup]
    publication_available: bool = False
    limits: dict[str, int] = Field(default_factory=dict)
    revision: str


DEMUXERS = {
    "m4b": "mov",
    "m4a": "mov",
    "mp3": "mp3",
    "flac": "flac",
    "ogg": "ogg",
    "opus": "ogg",
    "aac": "aac",
    "wav": "wav",
    "wma": "asf",
}
ARCHIVES = {"zip", "rar", "7z", "tar", "gz"}
DISC = re.compile(r"^(?:cd|disc|disk)\s*(\d+)$", re.I)
PART = re.compile(r"^part\s*(\d+)$", re.I)
TAG_NAMES = (
    "title,album,artist,album_artist,composer,narrator,track,disc,date,year,"
    "language,isbn,isbn10,isbn13,isbn_10,isbn_13,asin,abridged,series,series-part"
)


def probe_audio(fd, extension, deadline, executable="ffprobe"):
    # Inherited descriptor pins the opened source; never pass a source-supplied URL.
    input_path = f"/proc/self/fd/{fd}" if Path("/proc/self/fd").exists() else f"/dev/fd/{fd}"
    command = [
        executable,
        "-v",
        "error",
        "-max_alloc",
        "67108864",
        "-protocol_whitelist",
        "file",
        "-f",
        DEMUXERS[extension],
        "-probesize",
        "8388608",
        "-analyzeduration",
        "10000000",
    ]
    if DEMUXERS[extension] == "mov":
        command += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    command += [
        "-show_entries",
        f"format=duration,format_name:format_tags={TAG_NAMES}:"
        f"stream=codec_name,codec_type,duration:stream_tags={TAG_NAMES}",
        "-of",
        "json",
        input_path,
    ]
    output = probe_output(command, fd, deadline, label="Audio")
    data = json.loads(output)
    streams = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"]
    if len(streams) != 1 or not streams[0].get("codec_name"):
        raise InspectionError("Expected one identifiable audio stream")
    duration = float(data.get("format", {}).get("duration") or streams[0].get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise InspectionError("Audio duration could not be established")
    tags = {
        key.lower(): str(value)[:600]
        for source in (streams[0], data.get("format", {}))
        for key, value in source.get("tags", {}).items()
    }
    return {"codec": streams[0]["codec_name"], "duration": duration, "tags": tags}


def inspect_epub(fd):
    check_zip_directory(fd, "EPUB")
    with os.fdopen(os.dup(fd), "rb") as source, zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        names = {entry.filename for entry in entries}
        if len(names) != len(entries):
            raise InspectionError("EPUB contains conflicting duplicate entries")
        if "META-INF/encryption.xml" in names:
            raise InspectionError("EPUB declares encrypted resources; review is required")

        def read(name):
            relative_parts(name)
            info = archive.getinfo(name)
            if info.flag_bits & 1 or info.file_size > 1024 * 1024:
                raise InspectionError("EPUB metadata is encrypted or exceeds supported limits")
            return archive.read(info)

        if read("mimetype").strip() != b"application/epub+zip":
            raise InspectionError("File does not declare an EPUB container")
        container = ElementTree.fromstring(read("META-INF/container.xml"))
        rootfiles = container.findall(".//{*}rootfile")
        if len(rootfiles) != 1:
            raise InspectionError("EPUB needs one unambiguous package document")
        package_path = rootfiles[0].attrib["full-path"]
        package = ElementTree.fromstring(read(package_path))
        metadata = package.find("{*}metadata")
        manifest = package.find("{*}manifest")
        spine = package.find("{*}spine")
        if metadata is None or manifest is None or spine is None or len(spine) == 0:
            raise InspectionError("EPUB has no readable metadata and book spine")
        items = {item.attrib.get("id"): item for item in manifest}
        for ref in spine:
            item = items.get(ref.attrib.get("idref"))
            if item is None:
                raise InspectionError("EPUB spine references missing content")
            href = urlsplit(item.attrib.get("href", ""))
            if href.scheme or href.netloc:
                raise InspectionError("EPUB spine references external content")
            relative = unquote(href.path)
            relative_parts(relative)
            path = str(PurePosixPath(package_path).parent / relative)
            if path not in names or archive.getinfo(path).file_size == 0:
                raise InspectionError("EPUB spine content is missing or empty")
            if archive.getinfo(path).flag_bits & 1:
                raise InspectionError("EPUB spine content is encrypted")

        def values(name):
            return [
                node.text.strip()[:600]
                for node in metadata.findall(f"{{*}}{name}")
                if node.text and node.text.strip()
            ][:30]

        return {
            "title": next(iter(values("title")), None),
            "authors": values("creator"),
            "languages": values("language"),
            "identifiers": values("identifier"),
            "identifier_assertions": [
                {
                    "scheme": node.attrib.get("{http://www.idpf.org/2007/opf}scheme")
                    or node.attrib.get("scheme"),
                    "value": node.text.strip()[:600],
                }
                for node in metadata.findall("{*}identifier")
                if node.text and node.text.strip()
            ][:30],
            "spine_entries": len(spine),
        }


def inspect_file(fd, path, deadline):
    extension = PurePosixPath(path).suffix.lower().lstrip(".")
    result = {"path": path, "extension": extension, "state": "held", "medium": None}
    try:
        if extension in DEMUXERS:
            result.update(medium="audio", technical=probe_audio(fd, extension, deadline))
        elif extension == "epub":
            result.update(medium="ebook", metadata=inspect_epub(fd))
        elif extension in {"pdf", "cbz"}:
            output = probe_output(
                [sys.executable, "-m", "app.importing.ebook_probe", str(fd), extension],
                fd,
                deadline,
                label=extension.upper(),
            )
            data = json.loads(output)
            if "error" in data:
                raise InspectionError(data["error"])
            result.update(medium="ebook", metadata=data["metadata"])
        else:
            result["reason"] = (
                "Archive extraction is not enabled"
                if extension in ARCHIVES
                else "No supported content inspector for this file; review as media or extra"
            )
            return result
        result["state"] = "inspected"
    except FileNotFoundError:
        result["reason"] = "Audio inspection requires ffprobe on the worker"
    except (
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        RuntimeError,
        subprocess.TimeoutExpired,
        ElementTree.ParseError,
    ) as error:
        result["reason"] = (
            str(error) if isinstance(error, InspectionError) else "Invalid media metadata"
        )
    return result


def number(value, maximum):
    match = re.fullmatch(r"\s*(\d+)(?:/\d+)?\s*", str(value or ""))
    if match and 0 < int(match[1]) <= maximum:
        return int(match[1])
    return None


def folder_part(name):
    """(N, base title) for a folder holding one part of a book released in parts."""
    if match := PART.fullmatch(name):
        return int(match[1]), None
    labels = parse_title_labels(name)
    if labels.part and labels.part_total and labels.part_total >= 2:
        return labels.part, " ".join(re.findall(r"\w+", labels.title.casefold()))
    return None


def part_folders(files):
    """Part folders to publish as discs of one item: two or more parts of one book side by side.

    Audiobookshelf reads a single item's folder of disc subfolders, so a release holding
    every part becomes one item. A lone part folder stays as its own item.
    """
    siblings = {}
    for file in files:
        if file["state"] != "inspected" or file["medium"] != "audio":
            continue
        folder = PurePosixPath(file["path"]).parent
        if folder.name and (part := folder_part(folder.name)):
            siblings.setdefault(folder.parent, {})[folder] = part
    lifted = {}
    for members in siblings.values():
        numbers = [number for number, _ in members.values()]
        if len(members) > 1 and len(set(numbers)) == len(numbers):
            if len({title for _, title in members.values()}) == 1:
                lifted.update({folder: number for folder, (number, _) in members.items()})
    return lifted


def suggest_groups(files):
    groups = {}
    parts = part_folders(files)
    for file in files:
        if file["state"] != "inspected":
            continue
        path = PurePosixPath(file["path"])
        if file["medium"] == "ebook":
            key = ("ebook", str(path))
            metadata = file["metadata"]
            title, authors = metadata["title"], metadata["authors"]
            narrator, disc, track = None, None, None
        else:
            tags = file["technical"]["tags"]
            folder = path.parent
            disc_match = DISC.fullmatch(folder.name)
            part = parts.get(folder)
            if disc_match or part:
                folder = folder.parent
            # Album/narrator evidence separates differently tagged books in a flat pack.
            # Conflicting directories are never collapsed solely on a title match.
            title = tags.get("album")
            if part and title:
                title = parse_title_labels(title).title
            narrator = tags.get("narrator") or tags.get("composer")
            authors = [tags.get("album_artist") or tags.get("artist")]
            authors = [author for author in authors if author]
            key = ("audio", str(folder), title, narrator, tuple(authors), file["extension"])
            disc = number(tags.get("disc"), 999)
            folder_disc = int(disc_match[1]) if disc_match else part
            if part and disc in (1, part):
                # Parts restart their own disc numbering; the part becomes the disc.
                disc = None
            if folder_disc and disc and folder_disc != disc:
                file["state"], file["reason"] = "held", "Disc folder conflicts with embedded tags"
                continue
            disc = disc or folder_disc
            track = number(tags.get("track"), 999999)
        stable_key = fingerprint(key)
        group = groups.setdefault(
            stable_key,
            {
                "key": stable_key,
                "medium": file["medium"],
                "title": title,
                "authors": authors,
                "narrators": [narrator] if narrator else [],
                "files": [],
                "identity": "unresolved",
                "full_content": "unverified",
            },
        )
        group["files"].append({"path": str(path), "disc": disc, "track": track})
    return list(groups.values())


def inspect_download(root: Path, relative: str, *, max_bytes=200 * 1024**3, timeout=300):
    deadline = time.monotonic() + timeout
    with ExitStack() as scopes:
        mount = scopes.enter_context(directory(root))
        parent = scopes.enter_context(source_scope(mount, relative, "file"))
        leaf = relative_parts(relative)[-1]
        selected = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(selected.st_mode):
            kind = "directory"
            folder = scopes.enter_context(beneath(parent, leaf, folder=True))
        elif stat.S_ISREG(selected.st_mode):
            kind, folder = "file", parent
        else:
            raise InspectionError("Download contains a symlink or special file")

        def listing_now():
            if kind == "directory":
                return enumerate_files(folder)
            with beneath(folder, leaf) as fd:
                return [(leaf, identity(os.fstat(fd)))]

        root_identity = identity(os.fstat(folder))
        listing = listing_now()
        if sum(info["size"] for _, info in listing) > max_bytes:
            raise InspectionError("Download exceeds the supported inspection byte budget")
        files = []
        for path, expected in listing:
            with beneath(folder, path) as fd:
                if identity(os.fstat(fd)) != expected:
                    raise InspectionError("Source changed since directory enumeration")
                sha256 = digest(fd, deadline)
                inspected = inspect_file(fd, path, deadline)
                if identity(os.fstat(fd)) != expected:
                    raise InspectionError("Source changed while being inspected")
                files.append({**inspected, "identity": expected, "sha256": sha256})
        if listing_now() != listing:
            raise InspectionError(
                "Download changed during inspection; inspect it again when stable"
            )
        # Reopen from the configured root to detect a renamed/replaced directory.
        with source_scope(mount, relative, kind) as current:
            current_identity = identity(os.fstat(current))
            # Sibling downloads may change a single file's parent mtime/size.
            # Its directory identity and the selected file must still agree.
            keys = ("device", "inode") if kind == "file" else root_identity.keys()
            if any(current_identity[key] != root_identity[key] for key in keys):
                raise InspectionError("Download directory changed during inspection")
            if kind == "file":
                with beneath(current, leaf) as fd:
                    if identity(os.fstat(fd)) != listing[0][1]:
                        raise InspectionError("Selected download file changed during inspection")
        snapshot = {
            "schema_version": 1,
            "source_path": str(root),
            "relative_path": relative,
            "directory_identity": root_identity,
            "files": files,
            "groups": suggest_groups(files),
            "publication_available": False,
            "limits": {"max_bytes": max_bytes, "timeout_seconds": timeout},
        }
        if kind == "file":
            snapshot["source_kind"] = "file"
        return {**snapshot, "revision": fingerprint(snapshot)}
