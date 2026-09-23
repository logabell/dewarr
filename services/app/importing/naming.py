"""Deterministic per-version destination planning. This module never touches files."""

import hashlib
import re
import unicodedata
from collections import Counter
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(max_length=600)]
TOKEN = re.compile(r"\{([a-z_]+)\}")
OPTIONAL = re.compile(r"\[([^\[\]]*)\]")
TOKENS = {
    "author": "Filing author; Unknown author when absent",
    "author_sort": "Explicit sort name, falling back to the filing author",
    "title": "Book title",
    "subtitle": "Subtitle",
    "series": "Selected filing series",
    "sequence": "Series position; decimals and nonnumeric positions preserved",
    "part": "Part N of M, for one part of a book released in parts",
    "original_year": "Original work publication year",
    "edition_year": "Ebook edition year",
    "recording_year": "Audiobook recording release year",
    "year": "Ebook edition year or audiobook recording year",
    "edition": "Edition label",
    "publisher": "Edition publisher",
    "narrator": "Narrators of this recording",
    "language": "Version language",
    "abridgment": "Known abridgment status",
    "isbn": "Real ISBN",
    "asin": "Real ASIN",
    "disc": "Verified disc number",
    "track": "Verified track number",
    "original_name": "Original basename without extension",
    "format": "Actual file extension, without changing the file type",
    "formats": "Selected media formats for this item",
    "source": "Source provider",
    "release_id": "Original source release ID",
    "release_title": "Original source release title",
    "source_posted_year": "Tracker posting year",
}
FILE_TOKENS = {"disc", "track", "original_name", "format"}
AUDIO = {"m4b", "m4a", "mp3", "flac", "ogg", "opus", "aac", "wav", "wma"}
EBOOK = {"epub", "pdf", "mobi", "azw3", "cbr", "cbz"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def template_tokens(template):
    return set(TOKEN.findall(template))


def validate_template(template, *, folder):
    if not template.strip() or len(template) > 600:
        raise ValueError("Enter a naming template with at most 600 characters")
    if (
        template.startswith(("/", "\\"))
        or "\\" in template
        or any(unicodedata.category(char) in {"Cc", "Cf"} for char in template)
    ):
        raise ValueError("Templates must be relative paths without control characters")
    if not folder and "/" in template:
        raise ValueError("A filename template cannot create folders")
    if set(template_tokens(template)) - TOKENS.keys():
        raise ValueError("Unknown naming token")
    if folder and template_tokens(template) & FILE_TOKENS:
        raise ValueError("File-specific tokens cannot define a book's folder")
    for block in OPTIONAL.findall(template):
        if not TOKEN.search(block):
            raise ValueError("An optional segment must contain a metadata token")
    without_optional = OPTIONAL.sub("", template)
    if "[" in without_optional or "]" in without_optional:
        raise ValueError("Optional segments cannot be nested")
    if "{" in TOKEN.sub("", template) or "}" in TOKEN.sub("", template):
        raise ValueError("Use tokens such as {title}; expressions are not supported")
    if any(segment in {".", ".."} for segment in template.split("/")):
        raise ValueError("Relative traversal is not a naming segment")
    if "title" not in template_tokens(template):
        raise ValueError("Include {title} in the book folder and filename")


class NamingProfile(StrictModel):
    layout: Literal["conventional", "nested"] = "conventional"
    rename_files: bool = True
    merge_mp3_chapters: bool = Field(
        default=False,
        description=(
            "Merge a multi-file MP3 audiobook into one chapterized M4B before library import"
        ),
    )
    audio_folder: str = (
        "{author}/[{series}/][{sequence} - ][{recording_year} - ]{title}[ - {narrator}]"
    )
    ebook_folder: str = (
        "{author}/[{series}/][{sequence} - ][{edition_year} - ]{title}[ - {edition}]"
    )
    audio_filename: str = "[{disc}-][{track} - ]{title}"
    ebook_filename: str = "{title}"

    @model_validator(mode="after")
    def valid_templates(self):
        for medium in ("audio", "ebook"):
            validate_template(getattr(self, medium + "_folder"), folder=True)
            validate_template(getattr(self, medium + "_filename"), folder=False)
        return self


class NamingMetadata(StrictModel):
    title: Text
    authors: list[Text] = Field(default_factory=list, max_length=30)
    author_sort: Text | None = None
    subtitle: Text | None = None
    series: Text | None = None
    sequence: Text | None = None
    original_year: int | None = Field(default=None, ge=0, le=9999)
    edition_year: int | None = Field(default=None, ge=0, le=9999)
    recording_year: int | None = Field(default=None, ge=0, le=9999)
    edition: Text | None = None
    publisher: Text | None = None
    narrators: list[Text] = Field(default_factory=list, max_length=30)
    language: Text | None = None
    abridged: bool | None = None
    isbn: Text | None = None
    asin: Text | None = None
    source: Text | None = None
    release_id: Text | None = None
    release_title: Text | None = None
    source_posted_year: int | None = Field(default=None, ge=0, le=9999)
    # One part of a book released in parts. Its library item is labelled with the part.
    part_index: int | None = Field(default=None, ge=1, le=20)
    part_total: int | None = Field(default=None, ge=2, le=20)

    @model_validator(mode="after")
    def whole_part(self):
        if (self.part_index is None) != (self.part_total is None) or (
            self.part_index and self.part_index > self.part_total
        ):
            raise ValueError("A part needs a part number within the number of parts")
        return self

    @property
    def part_label(self):
        return f"Part {self.part_index} of {self.part_total}" if self.part_index else None


class PlannedSourceFile(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    role: Literal["media", "supplement"] = "media"
    complete: bool = True
    track: int | None = Field(default=None, ge=1, le=999999)
    disc: int | None = Field(default=None, ge=1, le=999)

    @model_validator(mode="after")
    def relative_path(self):
        parts = self.path.split("/")
        if (
            self.path.startswith("/")
            or "\\" in self.path
            or any(part in {"", ".", ".."} for part in parts)
            or any(unicodedata.category(char) in {"Cc", "Cf"} for char in self.path)
        ):
            raise ValueError(
                "Source paths must be relative without traversal or control characters"
            )
        return self


class ImportGroup(StrictModel):
    id: UUID
    work_id: UUID
    version_id: UUID
    medium: Literal["ebook", "audio"]
    metadata: NamingMetadata
    files: list[PlannedSourceFile] = Field(min_length=1, max_length=5000)
    decision: Literal["import", "skip-owned", "needs-review"] = "import"
    full_content: bool = True
    reason: Text | None = None


class FileMapping(StrictModel):
    source: str
    destination: str
    role: str


class PlannedConversion(StrictModel):
    converter: Literal["ffmpeg-chapterized-m4b"]
    output_name: str
    sources: list[str] = Field(min_length=2, max_length=2000)


class PlannedItem(StrictModel):
    group_id: UUID
    work_id: UUID
    version_id: UUID
    medium: str
    title: str
    state: Literal["ready", "held", "skipped"]
    reason: str | None = None
    folder: str | None = None
    files: list[FileMapping] = Field(default_factory=list)
    missing_metadata: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conversion: PlannedConversion | None = None


class ImportPlan(StrictModel):
    items: list[PlannedItem]
    expected_items: int
    held_items: int
    skipped_items: int
    publication_available: bool = False
    profile_revision: str


def fingerprint(value):
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def component(value, limit=180):
    value = unicodedata.normalize("NFC", str(value))
    value = "".join(
        " " if char in '/\\<>:"|?*' or unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in value
    )
    value = " ".join(value.split()).strip(" .")
    if not value:
        raise ValueError("A required name becomes empty after sanitizing")
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    if len(value.encode()) > limit:
        suffix = "~" + hashlib.sha256(value.encode()).hexdigest()[:12]
        value = (
            value.encode()[: limit - len(suffix)].decode("utf-8", errors="ignore").rstrip(" .")
            + suffix
        )
    return value


def values_for(metadata, file=None, *, medium=None):
    values = metadata.model_dump()
    if medium == "audio":
        values["year"] = metadata.recording_year
    elif medium == "ebook":
        values["year"] = metadata.edition_year
    values["author"] = next(
        (author for author in metadata.authors if author.strip()), "Unknown author"
    )
    values["author_sort"] = metadata.author_sort or values["author"]
    values["part"] = metadata.part_label
    values["narrator"] = ", ".join(metadata.narrators) or None
    values["abridgment"] = (
        "Abridged"
        if metadata.abridged is True
        else "Unabridged"
        if metadata.abridged is False
        else None
    )
    if metadata.sequence and re.fullmatch(r"\d+(?:\.\d+)?", metadata.sequence):
        whole, dot, fraction = metadata.sequence.partition(".")
        values["sequence"] = whole.zfill(2) + dot + fraction
    if file:
        values.update(
            track=f"{file.track:03}" if file.track else None,
            disc=f"{file.disc:02}" if file.disc else None,
            original_name=PurePosixPath(file.path).stem,
            format=PurePosixPath(file.path).suffix[1:].upper(),
        )
    return {
        key: component(value) if value is not None and str(value).strip() else None
        for key, value in values.items()
        if key in TOKENS
    }


def render(template, values):
    def optional(match):
        block = match.group(1)
        return block if all(values.get(key) for key in TOKEN.findall(block)) else ""

    template = OPTIONAL.sub(optional, template)

    def token(match):
        value = values.get(match.group(1))
        if not value:
            raise ValueError(f"Required metadata is missing: {match.group(1)}")
        return value

    rendered = TOKEN.sub(token, template)
    parts = rendered.split("/")
    if any(part.strip() in {"", ".", ".."} for part in parts):
        raise ValueError("The naming template produces an empty or unsafe folder")
    return "/".join(component(part) for part in parts)


def collision_key(path):
    return unicodedata.normalize("NFKC", path).casefold()


def plan_import(groups: list[ImportGroup], profile: NamingProfile, *, combine_parts=True):
    if not groups or len(groups) > 100 or sum(len(group.files) for group in groups) > 10000:
        raise ValueError("Preview 1–100 book groups and at most 10,000 files at a time")
    if len({group.id for group in groups}) != len(groups):
        raise ValueError("Each book group needs a distinct ID")
    # Different parts of one recording share its version.
    versions = Counter(
        (group.medium, group.version_id, group.metadata.part_index)
        for group in groups
        if group.decision == "import"
    )
    source_counts = Counter(
        file.path for group in groups if group.decision == "import" for file in group.files
    )
    used_folders = set()
    items = []
    for group in sorted(groups, key=lambda group: str(group.id)):
        item = PlannedItem(
            group_id=group.id,
            work_id=group.work_id,
            version_id=group.version_id,
            medium=group.medium,
            title=group.metadata.title,
            state="ready",
        )
        items.append(item)
        if group.decision == "skip-owned":
            item.state, item.reason = (
                "skipped",
                group.reason or "Requested version is already available",
            )
            continue
        try:
            if group.decision == "needs-review" or not group.full_content:
                raise ValueError(
                    group.reason or "Resolve the book identity and full content before importing"
                )
            if any(not file.complete for file in group.files):
                raise ValueError("Wait for every selected file to finish downloading")
            if versions[group.medium, group.version_id, group.metadata.part_index] > 1:
                raise ValueError("Choose one representation for this version")
            if any(source_counts[file.path] > 1 for file in group.files):
                raise ValueError("A source file belongs to more than one selected group")
            media = [file for file in group.files if file.role == "media"]
            if not media:
                raise ValueError("Supplementary files alone do not form a complete book")
            allowed = AUDIO if group.medium == "audio" else EBOOK
            if any(PurePosixPath(file.path).suffix[1:].lower() not in allowed for file in media):
                raise ValueError("Selected media has an unsupported or inconsistent extension")
            if any(
                PurePosixPath(file.path).suffix[1:].lower() not in EBOOK
                for file in group.files
                if file.role == "supplement"
            ):
                raise ValueError("Only identified supplementary ebooks may join this item")
            merging = False
            if group.medium == "audio" and len(media) > 1:
                order = [(file.disc or 1, file.track) for file in media]
                if any(file.track is None for file in media) or len(set(order)) != len(order):
                    raise ValueError("Verify unique disc and track ordering for this recording")
                if len({PurePosixPath(file.path).suffix.lower() for file in media}) > 1:
                    raise ValueError(
                        "Choose one audio representation; do not combine alternate encodings"
                    )
                from app.importing.converters import MAX_CHAPTERS, mp3_chapter_merge

                merging = mp3_chapter_merge(profile, media)
                if merging and len(media) > MAX_CHAPTERS:
                    raise ValueError(
                        "This recording has more MP3 files than chapter merging supports"
                    )
                if profile.rename_files and not merging:
                    tokens = template_tokens(profile.audio_filename)
                    if "track" not in tokens or (
                        len({file.disc or 1 for file in media}) > 1 and "disc" not in tokens
                    ):
                        raise ValueError(
                            "Include track and, for multiple discs, disc tokens "
                            "to preserve playback order"
                        )
            values = values_for(group.metadata, medium=group.medium)
            values["formats"] = (
                "M4B"
                if merging
                else " + ".join(
                    sorted({PurePosixPath(file.path).suffix[1:].upper() for file in media})
                )
            )
            template = getattr(profile, group.medium + "_folder")
            item.missing_metadata = sorted(
                key for key in template_tokens(template) if not values.get(key)
            )
            if not group.metadata.authors:
                item.missing_metadata.append("author")
            folder = render(template, values)
            if values["part"] and "part" not in template_tokens(template):
                # Audiobookshelf and Grimmory keep each folder as its own item. The label
                # keeps a lone part distinct, and Dewarr groups the parts under one book.
                parents, _, leaf = folder.rpartition("/")
                leaf = component(f"{leaf} ({values['part']})")
                folder = "/".join(part for part in (parents, leaf) if part)
                item.warnings.append(
                    f"{group.metadata.part_label}: kept as its own library item until every "
                    "part is in the library; in Audiobookshelf, Dewarr then combines them "
                    "into one book with disc folders"
                    if combine_parts and group.medium == "audio"
                    else f"{group.metadata.part_label}: kept as its own library item, "
                    "grouped with the other parts in Dewarr"
                )
            if profile.layout == "nested":
                parents, _, leaf = folder.rpartition("/")
                book_folder = render("[{sequence} - ]{title}", values)
                folder = "/".join(part for part in (parents, book_folder, leaf) if part)
                item.warnings.append(
                    "Nested layout is preview-only until Audiobookshelf scanner certification"
                )
            if re.fullmatch(r"(?:disc|cd)\s*\d+", folder.split("/")[-1], re.I):
                raise ValueError("A book version folder must not use disc-grouping names")
            root = "audiobooks" if group.medium == "audio" else "ebooks"
            key = collision_key(root + "/" + folder)
            if key in used_folders:
                parts = folder.split("/")
                parts[-1] = component(parts[-1], 140) + " [" + group.version_id.hex + "]"
                folder = "/".join(parts)
                key = collision_key(root + "/" + folder)
                item.warnings.append("Added a stable version suffix to avoid a name collision")
            if key in used_folders or len(folder.encode()) > 1000:
                raise ValueError("Destination folder collides or exceeds the relative path limit")
            files = []
            names = set()
            ordered = sorted(
                group.files, key=lambda file: (file.disc or 1, file.track or 0, file.path)
            )
            for file in ordered:
                if merging and file.role == "media":
                    continue
                extension = PurePosixPath(file.path).suffix.lower()
                name = (
                    render(
                        getattr(profile, group.medium + "_filename"),
                        {**values, **values_for(group.metadata, file, medium=group.medium)},
                    )
                    if profile.rename_files
                    else component(PurePosixPath(file.path).stem)
                )
                if collision_key(name + extension) in names:
                    name = (
                        component(name, 150)
                        + " ["
                        + hashlib.sha256(file.path.encode()).hexdigest()[:16]
                        + "]"
                    )
                if collision_key(name + extension) in names:
                    raise ValueError("Selected files produce the same destination")
                names.add(collision_key(name + extension))
                files.append(
                    FileMapping(
                        source=file.path,
                        destination=f"{root}/{folder}/{name}{extension}",
                        role=file.role,
                    )
                )
            if merging:
                output = component(values["title"]) + ".m4b"
                if collision_key(output) in names:
                    output = (
                        component(values["title"], 140)
                        + " ["
                        + hashlib.sha256(b"m4b").hexdigest()[:8]
                        + "].m4b"
                    )
                if collision_key(output) in names:
                    raise ValueError("Selected files produce the same destination")
                sources = [file.path for file in ordered if file.role == "media"]
                files.append(
                    FileMapping(
                        source=sources[0],
                        destination=f"{root}/{folder}/{output}",
                        role="media",
                    )
                )
                item.conversion = PlannedConversion(
                    converter="ffmpeg-chapterized-m4b",
                    output_name=output,
                    sources=sources,
                )
                item.warnings.append(
                    "These MP3 files will be merged into one chapterized M4B before import"
                )
            used_folders.add(key)
            item.folder, item.files = f"{root}/{folder}", files
            if group.medium == "ebook" and len(media) > 1:
                item.warnings.append(
                    "One ebook will be primary in Audiobookshelf; other formats are supplementary"
                )
        except ValueError as error:
            item.state, item.reason = "held", str(error)
            item.folder, item.files, item.conversion = None, [], None
    ready = [item for item in items if item.state == "ready"]
    for item in ready:
        own = collision_key(item.folder)
        if any(
            other is not item
            and (
                own.startswith(collision_key(other.folder) + "/")
                or collision_key(other.folder).startswith(own + "/")
            )
            for other in ready
        ):
            item.state, item.reason = (
                "held",
                "One book folder would contain another book; choose distinct item folders",
            )
    for item in items:
        if item.state == "held":
            item.folder, item.files, item.conversion = None, [], None
    return ImportPlan(
        items=items,
        expected_items=sum(item.state == "ready" for item in items),
        held_items=sum(item.state == "held" for item in items),
        skipped_items=sum(item.state == "skipped" for item in items),
        profile_revision=fingerprint(profile.model_dump()),
    )
