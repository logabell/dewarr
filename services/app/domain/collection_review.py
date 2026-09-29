"""Owner-scoped collection review: uploader claims → catalog choices → frozen files."""

from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import array

from app.adapters.catalog_types import BookData
from app.adapters.contracts import AdapterError
from app.adapters.source_releases import release_value
from app.db.models import (
    Operation,
    SourceArtifact,
    SourceConnection,
    SourceResult,
    Work,
    WorkMetadataSource,
)
from app.domain import book_sources, collection_contents
from app.domain.automatic_eligibility import AUDIO, EBOOKS, collection_candidate
from app.domain.catalog_metadata import import_book
from app.domain.operations import transaction_lock
from app.domain.title_matching import compatible_title
from app.domain.visibility import visible_work
from app.domain.work_graph import family_ids
from app.importing.naming import fingerprint

KIND = "collection.review"


class CollectionCandidate(BaseModel):
    id: str
    title: str
    authors: list[str]
    cover_url: str | None = None
    external_id: str | None = None
    work_id: UUID | None = None
    series: list[dict] = []
    owned: bool = False


class PackContentsEntry(BaseModel):
    id: str
    title: str
    candidates: list[CollectionCandidate] = []
    match: str
    files: list[str] = []
    evidence: list[dict] = []
    recordings: list[dict] = []


class CollectionFile(BaseModel):
    path: str
    size_bytes: int


class CollectionPreview(BaseModel):
    review_id: UUID
    revision: str
    title: str
    possible_collection: bool
    requested_title: str
    entries: list[PackContentsEntry]
    files: list[CollectionFile] = []
    warnings: list[str] = []
    artifact_id: UUID | None = None
    bibliography_count: int = 0
    excluded: list[dict] = []
    series_coverage: list[dict] = []


class CollectionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str
    candidate_id: str
    paths: list[str] = Field(min_length=1, max_length=10000)


class CollectionDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    choices: list[CollectionChoice] = Field(min_length=1, max_length=100)
    download_all_files: bool = False


class CollectionReceipt(BaseModel):
    attempt_id: UUID
    message: str


async def context(db, user, search_id, result_id):
    search, changed = await book_sources.checked(db, search_id, user.id)
    row = await db.get(SourceResult, result_id)
    if not row or row.owner_id != user.id or row.operation_id != search.id:
        raise HTTPException(404, "Source result not found")
    source = await db.get(SourceConnection, row.source_key)
    if (
        changed
        or row.expires_at <= datetime.now(UTC)
        or not source
        or not source.enabled
        or source.generation != row.source_generation
    ):
        raise HTTPException(409, "This source result changed or expired. Search again.")
    return search, row


def file_matches(path, title):
    """Whole normalized title phrase; numeric track names alone establish nothing."""
    key = collection_contents.title_key(title)
    path_key = collection_contents.title_key(str(PurePosixPath(path).with_suffix("")))
    return bool(key and (" " + key + " ") in (" " + path_key + " "))


async def preview(db, user, search_id, result_id, artifact_id=None):
    search, row = await context(db, user, search_id, result_id)
    owner_id = user.id
    raw = dict(row.release_snapshot)
    generation = row.source_generation
    work = dict(search.payload["work"])
    release = release_value(row.source_key, raw)
    parsed = release.details.get("collection_contents") if release.source == "mam" else None
    if not parsed or parsed.get("parser_version") != collection_contents.PARSER_VERSION:
        parsed = collection_contents.extract(release.description or "")
    warnings = []
    metadata_id = await db.scalar(
        select(WorkMetadataSource.external_id)
        .where(
            WorkMetadataSource.work_id.in_(family_ids(UUID(work["id"]))),
            WorkMetadataSource.provider == "hardcover",
            WorkMetadataSource.accepted.is_(True),
        )
        .limit(1)
    )
    bibliography = {"books": [], "truncated": False}
    if metadata_id:
        from app.api.metadata import provider_call

        try:
            bibliography, stale, warning = await provider_call(
                db, owner_id, "hardcover", "collection_bibliography", metadata_id
            )
            if stale or warning:
                warnings.append(warning or "The bibliography is cached; verify catalog choices.")
        except (AdapterError, HTTPException):
            warnings.append(
                "Hardcover bibliography is unavailable. "
                "Showing matching books already in your catalog."
            )
    # provider_call rolls back before I/O; reload all security-sensitive context.
    from app.db.models import User

    user = await db.get(User, owner_id, populate_existing=True)
    search, row = await context(db, user, search_id, result_id)
    if row.release_snapshot != raw or row.source_generation != generation:
        raise HTTPException(409, "Source details changed; review again")
    local = (
        list(
            await db.scalars(
                select(Work)
                .where(
                    visible_work(user),
                    Work.redirect_to.is_(None),
                    Work.authors.has_any(array(work["authors"])),
                )
                .limit(1001)
            )
        )
        if work["authors"]
        else []
    )
    candidates, documents = [], {}
    for book in bibliography["books"]:
        key = "hardcover:" + book["external_id"]
        candidate = CollectionCandidate(
            id=key,
            title=book["title"],
            authors=book["authors"],
            cover_url=book.get("cover_url"),
            external_id=book["external_id"],
            series=book.get("series", []),
        )
        candidates.append((candidate, [book["title"], *book.get("aliases", [])]))
        documents[key] = {"book": book}
    linked_ids = set(
        await db.scalars(
            select(WorkMetadataSource.work_id).where(
                WorkMetadataSource.provider == "hardcover",
                WorkMetadataSource.external_id.in_(
                    [b["external_id"] for b in bibliography["books"]]
                ),
                WorkMetadataSource.accepted.is_(True),
            )
        )
    )
    for book in local[:1000]:
        if book.id in linked_ids:
            continue
        key = "work:" + str(book.id)
        candidates.append(
            (
                CollectionCandidate(
                    id=key,
                    title=book.title,
                    authors=book.authors,
                    cover_url=book.cover_url,
                    work_id=book.id,
                ),
                [book.title],
            )
        )
        documents[key] = {"work_id": str(book.id)}
    from app.db.models import CatalogSeries, SeriesMembership
    from app.domain.availability import availability_for
    from app.domain.series_projection import project
    from app.domain.work_graph import canonical_map

    mapping = canonical_map()
    links = (
        await db.execute(
            select(WorkMetadataSource.external_id, mapping.c.work_id)
            .join(mapping, mapping.c.origin_id == WorkMetadataSource.work_id)
            .join(Work, Work.id == mapping.c.work_id)
            .where(
                WorkMetadataSource.provider == "hardcover",
                WorkMetadataSource.accepted.is_(True),
                visible_work(user),
                WorkMetadataSource.external_id.in_(
                    [c.external_id for c, _ in candidates if c.external_id]
                ),
            )
        )
    ).all()
    by_external = {}
    for external, work_id in links:
        by_external.setdefault(external, set()).add(work_id)
    for candidate, _ in candidates:
        if candidate.external_id and len(by_external.get(candidate.external_id, set())) == 1:
            candidate.work_id = next(iter(by_external[candidate.external_id]))
    available = await availability_for(
        db, user, [c.work_id for c, _ in candidates if c.work_id], identity_only=True
    )
    for candidate, _ in candidates:
        if candidate.work_id and release.medium in {"audio", "ebook"}:
            candidate.owned = getattr(available[candidate.work_id], release.medium)
    files = []
    if artifact_id:
        artifact = await db.get(SourceArtifact, artifact_id)
        if (
            not artifact
            or artifact.owner_id != owner_id
            or artifact.source_key != row.source_key
            or artifact.source_id != raw["source_id"]
            or artifact.source_generation != generation
        ):
            raise HTTPException(409, "Inspect this source result before choosing files")
        if artifact.descriptor.get("protocol") == "nzb":
            raise HTTPException(422, "Collection file selection requires a torrent")
        files = [CollectionFile(**f) for f in artifact.descriptor["files"]]
    entries = []
    for item in parsed["items"]:
        key = collection_contents.title_key(item["title"])
        exact = [
            c
            for c, labels in candidates
            if any(collection_contents.title_key(t) == key for t in labels)
        ]
        proposed = exact or [
            c
            for c, labels in candidates
            if any(
                compatible_title(item["title"], t, allow_extra_subtitle=True)
                or collection_contents.title_key(t).replace(" ", "") == key.replace(" ", "")
                for t in labels
            )
        ]
        entries.append(
            PackContentsEntry(
                id=fingerprint({"title": key}),
                title=item["title"],
                candidates=proposed[:20],
                match="exact" if len(exact) == 1 else "review" if proposed else "unmatched",
                evidence=item["evidence"],
                recordings=item["recordings"],
                files=[
                    f.path
                    for f in files
                    if file_matches(f.path, item["title"])
                    and PurePosixPath(f.path).suffix.lower().lstrip(".") in AUDIO | EBOOKS
                ],
            )
        )
    if not entries:
        warnings.append(
            "No explicit title list was found. Review the torrent files; "
            "file counts are not book counts."
        )
        # File names may propose titles only after explicit torrent inspection.
        if files:
            for candidate, labels in candidates:
                paths = [
                    f.path
                    for f in files
                    if any(file_matches(f.path, t) for t in labels)
                    and PurePosixPath(f.path).suffix.lower().lstrip(".") in AUDIO | EBOOKS
                ]
                if paths:
                    entries.append(
                        PackContentsEntry(
                            id=fingerprint({"candidate": candidate.id}),
                            title=candidate.title,
                            candidates=[candidate],
                            match="review",
                            files=paths,
                        )
                    )
    primary = [
        f.path for f in files if PurePosixPath(f.path).suffix.lower().lstrip(".") in AUDIO | EBOOKS
    ]
    if len(primary) == 1 and compatible_title(release.title, work["title"]):
        whole = next((c for c, _ in candidates if c.work_id == UUID(work["id"])), None)
        if whole and not any(e.title == whole.title for e in entries):
            entries.insert(
                0,
                PackContentsEntry(
                    id=fingerprint({"whole_book": whole.id}),
                    title=whole.title,
                    candidates=[whole],
                    match="review",
                    files=primary,
                ),
            )
            warnings.append(
                "This is one physical book file. "
                "The listed stories cannot be downloaded separately."
            )
    if bibliography["truncated"] or len(local) > 1000 or parsed["truncated"]:
        warnings.append(
            "The review reached its safety limit. "
            "This is a partial list; do not assume complete coverage."
        )
    coverage = []
    matched = {
        c.external_id
        for entry in entries
        if len(entry.candidates) == 1
        for c in entry.candidates
        if c.external_id
    }
    rows = list(
        await db.scalars(
            select(CatalogSeries).where(
                CatalogSeries.owner_id == owner_id,
                CatalogSeries.fetched_at.is_not(None),
                CatalogSeries.id.in_(
                    select(SeriesMembership.series_id).where(
                        SeriesMembership.present.is_(True),
                        SeriesMembership.snapshot["book"]["external_id"].astext.in_(matched),
                    )
                ),
            )
        )
    )
    for series in rows:
        pairs = (
            await db.execute(
                select(SeriesMembership, Work)
                .join(Work, Work.id == SeriesMembership.work_id)
                .where(
                    SeriesMembership.series_id == series.id,
                    SeriesMembership.present.is_(True),
                    visible_work(user),
                )
            )
        ).all()
        main, _ = project(pairs)
        ids = {e.snapshot["book"]["external_id"] for e, _ in main}
        covered = ids & matched
        if covered:
            coverage.append(
                {
                    "name": series.name,
                    "external_id": series.external_id,
                    "included": len(covered),
                    "total": len(ids),
                    "basis": "observed main books",
                }
            )
    evidence = {
        "search_id": str(search_id),
        "result_id": str(result_id),
        "source_generation": generation,
        "source_snapshot": raw,
        "artifact_id": str(artifact_id) if artifact_id else None,
        "artifact_sha256": artifact.sha256 if artifact_id else None,
        "work": work,
        "entries": [e.model_dump(mode="json") for e in entries],
        "files": [f.model_dump() for f in files],
        "documents": documents,
        "parser_version": collection_contents.PARSER_VERSION,
    }
    revision = fingerprint(evidence)
    await transaction_lock(db, f"collection-review:{owner_id}:{revision}")
    saved = await db.scalar(
        select(Operation).where(
            Operation.owner_id == owner_id,
            Operation.idempotency_key == "collection-review:" + revision,
        )
    )
    if not saved:
        saved = Operation(
            owner_id=owner_id,
            kind=KIND,
            status="completed",
            idempotency_key="collection-review:" + revision,
            message="Collection contents ready for review",
            payload=evidence,
        )
        db.add(saved)
        await db.flush()
    return CollectionPreview(
        review_id=saved.id,
        revision=revision,
        title=release.title,
        requested_title=work["title"],
        possible_collection=collection_candidate(release, work) or len(entries) > 1,
        entries=entries,
        files=files,
        warnings=warnings,
        artifact_id=artifact_id,
        bibliography_count=len(bibliography["books"]),
        excluded=parsed["excluded"],
        series_coverage=coverage,
    )


async def download(db, user, review_id, body, key):
    from app.domain import acquisition, acquisition_selection, automatic_routes, download_attempts
    from app.domain.permissions import auto_approves, download_authorization
    from app.domain.release_profiles import ProfileSnapshot
    from app.domain.work_graph import canonical_work

    await transaction_lock(db, f"operation:{user.id}:{key}")
    command = {"review_id": str(review_id), **body.model_dump(mode="json")}
    previous = await db.scalar(
        select(Operation).where(Operation.owner_id == user.id, Operation.idempotency_key == key)
    )
    if previous:
        if previous.kind != "collection.download" or previous.payload["command"] != command:
            raise HTTPException(409, "This command key belongs to another collection choice")
        return CollectionReceipt(**previous.payload["receipt"])
    review = await db.get(Operation, review_id)
    if not review or review.owner_id != user.id or review.kind != KIND:
        raise HTTPException(404, "Collection review not found")
    evidence = review.payload
    if fingerprint(evidence) != body.revision or review.created_at < datetime.now(UTC) - timedelta(
        hours=1
    ):
        raise HTTPException(409, "Collection review expired or changed; refresh it")
    search, row = await context(db, user, UUID(evidence["search_id"]), UUID(evidence["result_id"]))
    if (
        row.release_snapshot != evidence["source_snapshot"]
        or row.source_generation != evidence["source_generation"]
    ):
        raise HTTPException(409, "Source details changed; review again")
    artifact = (
        await db.get(SourceArtifact, UUID(evidence["artifact_id"]))
        if evidence["artifact_id"]
        else None
    )
    if (
        not artifact
        or artifact.owner_id != user.id
        or artifact.sha256 != evidence["artifact_sha256"]
    ):
        raise HTTPException(409, "Inspect the torrent files before downloading")
    if len({c.entry_id for c in body.choices}) != len(body.choices):
        raise HTTPException(422, "Choose each collection entry only once")
    entries = {e["id"]: e for e in evidence["entries"]}
    known_paths = {f["path"] for f in evidence["files"]}
    selected_paths, works, selections = set(), set(), []
    for choice in body.choices:
        entry = entries.get(choice.entry_id)
        if not entry or choice.candidate_id not in {c["id"] for c in entry["candidates"]}:
            raise HTTPException(422, "Choose a catalog candidate from this review")
        paths = set(choice.paths)
        if len(paths) != len(choice.paths) or not paths <= known_paths or paths & selected_paths:
            raise HTTPException(422, "Choose distinct inspected files for each book")
        selected_paths.update(paths)
    if body.download_all_files:
        selected_paths = known_paths
    medium = row.release_snapshot["medium"]
    if medium not in {"audio", "ebook"}:
        raise HTTPException(422, "Confirm this release's medium first")
    options = acquisition.RequestOptions(mode=medium)
    if not auto_approves(user, options):
        raise HTTPException(403, "This account needs download approval")
    grant = download_authorization.set(options)
    try:
        for index, choice in enumerate(body.choices):
            doc = evidence["documents"][choice.candidate_id]
            work = (
                await import_book(db, user, BookData.model_validate(doc["book"]))
                if "book" in doc
                else await book_sources.accessible_work(db, user, UUID(doc["work_id"]))
            )
            work = await canonical_work(db, work.id)
            if work.id in works:
                raise HTTPException(
                    422, "Alternate recordings of one book need separate reviewed downloads"
                )
            works.add(work.id)
            bound = search.payload.get("command", {}).get("request_id")
            if bound and str(work.id) == evidence["work"]["id"]:
                from app.domain.request_preferences import owned_request

                intent = await owned_request(db, user, UUID(bound), work.id)
            else:
                intent, _ = await acquisition.submit(
                    db,
                    user,
                    work.id,
                    options,
                    acquisition.RequestReason(),
                    f"collection-request:{key}:{index}",
                )
            outcomes = await acquisition.assess(
                db, user, work.id, acquisition.RequestSpec.model_validate(intent.specification)
            )
            slot = "either" if intent.specification["mode"] == "either" else medium
            if not any(t["slot"] == slot and t["state"] == "wanted" for t in outcomes):
                continue
            profile = ProfileSnapshot.model_validate(intent.release_policy)
            routes, _ = await automatic_routes.inherit(
                db,
                user,
                acquisition.RequestSpec.model_validate(intent.specification),
                profile,
                automatic_routes.AutomaticRoutes(),
                include_fallback=False,
            )
            selected = await acquisition_selection.prepare(
                db,
                user,
                acquisition_selection.SelectionInput(
                    intent_id=intent.id,
                    slot=slot,
                    artifact_id=artifact.id,
                    confirmed_work_id=work.id,
                    selected_paths=sorted(selected_paths),
                    **automatic_routes.selection_clients(routes, medium),
                ),
                f"collection-selection:{key}:{index}",
            )
            selected.frozen = {
                **selected.frozen,
                "collection_review": {
                    "review_id": str(review.id),
                    "revision": body.revision,
                    "paths": sorted(choice.paths),
                    "title": entries[choice.entry_id]["title"],
                },
            }
            selections.append(selected)
        if not selections:
            raise HTTPException(409, "These books are already available or have active requests")
        attempt = await download_attempts.start(
            db,
            user,
            selections[0].id,
            "collection-transfer:" + key,
            additional_selection_ids=[s.id for s in selections[1:]],
        )
    finally:
        download_authorization.reset(grant)
    receipt = CollectionReceipt(
        attempt_id=attempt.id,
        message=f"Queued one collection transfer for {len(selections)} reviewed books",
    )
    db.add(
        Operation(
            owner_id=user.id,
            kind="collection.download",
            status="completed",
            idempotency_key=key,
            message=receipt.message,
            payload={"command": command, "receipt": receipt.model_dump(mode="json")},
        )
    )
    return receipt
