"""Review file membership without changing byte inspection or catalog identity."""

from collections import Counter
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from app.db.models import InspectionGrouping
from app.importing.audio_order import inferred_tracks
from app.importing.inspection import InspectedGroup
from app.importing.naming import PlannedSourceFile, StrictModel, fingerprint


class ReviewedFile(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    role: Literal["media", "supplement"] = "media"
    disc: int | None = Field(default=None, ge=1, le=999)
    track: int | None = Field(default=None, ge=1, le=999999)


class ReviewedGroup(StrictModel):
    files: list[ReviewedFile] = Field(min_length=1, max_length=5000)
    same_edition: bool = False


class ExcludedFile(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=300)


class GroupingContent(StrictModel):
    groups: list[InspectedGroup]
    excluded: list[ExcludedFile]


def proposed(snapshot):
    assigned = {file["path"] for group in snapshot["groups"] for file in group["files"]}
    return GroupingContent(
        groups=[
            {**group, "files": inferred_tracks(group["files"])}
            if group["medium"] == "audio"
            else group
            for group in snapshot["groups"]
        ],
        excluded=[
            ExcludedFile(
                path=file["path"], reason=file.get("reason") or "Not proposed as book media"
            )
            for file in snapshot["files"]
            if file["path"] not in assigned
        ],
    )


def regroup(snapshot, groups, excluded):
    files = {file["path"]: file for file in snapshot["files"]}
    paths = [file.path for group in groups for file in group.files] + [
        file.path for file in excluded
    ]
    if len(paths) > 10000 or set(paths) != set(files):
        raise ValueError(
            "Assign or explicitly exclude every inspected file; unknown paths are not allowed"
        )
    if any(count != 1 for count in Counter(paths).values()):
        raise ValueError("Each file must belong to one group or one exclusion")
    if any(not item.reason.strip() for item in excluded):
        raise ValueError("Give a reason for each excluded file")
    original_keys = {
        fingerprint([PlannedSourceFile(**file).model_dump() for file in group["files"]]): group[
            "key"
        ]
        for group in snapshot["groups"]
    }
    result = []
    for group in groups:
        observations = [files[file.path] for file in group.files]
        if any(file["state"] != "inspected" for file in observations):
            raise ValueError("A held or unsupported file must pass byte inspection before grouping")
        main = [files[file.path] for file in group.files if file.role == "media"]
        supplements = [files[file.path] for file in group.files if file.role == "supplement"]
        media = {file["medium"] for file in main}
        if len(media) != 1 or not media <= {"ebook", "audio"}:
            raise ValueError("Keep ebook editions and audiobook recordings in separate groups")
        medium = next(iter(media))
        if supplements and (
            medium != "audio" or any(file["extension"] != "pdf" for file in supplements)
        ):
            raise ValueError("Only inspected PDF companions can be attached to an audiobook")
        if medium == "ebook" and len(main) > 1 and not group.same_edition:
            raise ValueError("Confirm that these ebook formats contain the same complete edition")
        if medium == "ebook" and len({file["extension"] for file in main}) != len(main):
            raise ValueError("Keep different ebooks of the same file format in separate groups")
        if medium == "audio" and len({file["extension"] for file in main}) != 1:
            raise ValueError("Do not combine alternate audio encodings into one recording")
        selected = [PlannedSourceFile(**file.model_dump()) for file in group.files]
        if any(
            (file.track or file.disc) and (medium == "ebook" or file.role == "supplement")
            for file in selected
        ):
            raise ValueError("Disc and track numbers apply only to audio media")
        audio_files = [file for file in selected if file.role == "media"]
        if medium == "audio" and len(audio_files) > 1:
            order = [(file.disc or 1, file.track) for file in audio_files]
            if any(file.track is None for file in audio_files) or len(set(order)) != len(order):
                raise ValueError("Assign unique disc and track numbers to every audio file")
        selected.sort(key=lambda file: (file.disc or 1, file.track or 1, file.path))

        def consensus(values, fallback):
            return values[0] if all(value == values[0] for value in values) else fallback

        if medium == "ebook":
            title, authors = (
                consensus([file["metadata"].get("title") for file in main], None),
                consensus([file["metadata"].get("authors", []) for file in main], []),
            )
            narrators = []
        else:
            tags = [file["technical"]["tags"] for file in main]
            title = consensus([value.get("album") for value in tags], None)
            author = consensus(
                [value.get("album_artist") or value.get("artist") for value in tags], None
            )
            narrator = consensus(
                [value.get("narrator") or value.get("composer") for value in tags], None
            )
            authors, narrators = [author] if author else [], [narrator] if narrator else []
        members = [file.model_dump() for file in selected]
        signature = fingerprint(members)
        result.append(
            InspectedGroup(
                key=original_keys.get(signature)
                or fingerprint({"medium": medium, "files": members}),
                medium=medium,
                title=title,
                authors=authors,
                narrators=narrators,
                files=selected,
                identity="unresolved",
                full_content="unverified",
                same_edition=medium == "ebook" and len(main) > 1,
            )
        )
    return GroupingContent(
        groups=sorted(result, key=lambda group: group.key),
        excluded=sorted(excluded, key=lambda file: file.path),
    )


async def latest_grouping(db, inspection_id):
    return await db.scalar(
        select(InspectionGrouping)
        .where(InspectionGrouping.inspection_id == inspection_id)
        .order_by(InspectionGrouping.position.desc())
        .limit(1)
    )


def resolved_grouping(inspection, latest):
    if latest:
        return latest.revision, GroupingContent.model_validate(latest.content)
    content = proposed(inspection.snapshot)
    revision = inspection.snapshot["revision"]
    # Old inspections can gain filename ordering without rewriting their byte
    # evidence. Invalidate previous review tokens if the proposed order changed.
    if any(
        old.get("track") != new.track
        for before, after in zip(inspection.snapshot["groups"], content.groups, strict=True)
        for old, new in zip(before["files"], after.files, strict=True)
    ):
        revision = fingerprint({"inspection": revision, "grouping": content.model_dump()})
    return revision, content


async def current_grouping(db, inspection):
    return resolved_grouping(inspection, await latest_grouping(db, inspection.id))
