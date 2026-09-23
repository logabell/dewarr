import hashlib
import json
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.audiobookshelf import ABSItem
from app.db.models import AssetContains, LibraryAsset, ProviderObject, Version, Work
from app.domain.catalog_language import catalog_language
from app.domain.catalog_titles import (
    credit_key,
    display_title,
    display_title_sql,
    identity_authors,
    parse_title_labels,
    recording_kind,
)
from app.domain.work_graph import canonical_work


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def work_key(title: str, authors: list[str]) -> str | None:
    if not authors:
        return None  # Title alone cannot identify a work.
    return hashlib.sha256(
        json.dumps(
            [
                normalized(title),
                sorted(normalized(author) for author in authors),
            ]
        ).encode()
    ).hexdigest()


def library_key(title: str, authors: list[str]) -> str | None:
    """Which book a library item is: its title without recording labels, by its authors.

    Cast and publisher credits are not authors. When those are all an item has, only
    parts of one recording ("1 of 3") are keyed together, by the same credit.
    """
    labels = parse_title_labels(title)
    kept, credits = identity_authors(authors)
    if kept:
        return work_key(labels.title, kept)
    if labels.part and credits:
        return work_key(labels.title, sorted(f"credit:{credit_key(name)}" for name in credits))
    return None


def _same_recording_label(title, authors, other_title, other_authors):
    label = display_title(title)
    # Title alone cannot identify a work, with or without an edition label.
    if not label or not authors or not other_authors:
        return False
    return label == display_title(other_title) and work_key(label, authors) == work_key(
        label, other_authors
    )


def _credited_author(item_authors, candidate_authors):
    """A dramatization can list its cast as authors. The catalog author must be among them."""
    theirs = {credit_key(name) for name in identity_authors(candidate_authors)[0]}
    ours = {credit_key(name) for name in identity_authors(item_authors)[0]}
    return bool(theirs) and theirs <= ours


def _prefer_catalog_book(candidates):
    """When a shelf title and its (Unabridged) copy both exist, keep the catalog book."""

    def rank(row):
        if not row.provisional and row.catalog_public:
            return 0
        if not row.provisional:
            return 1
        if row.catalog_public:
            return 2
        return 3

    best = min(rank(row) for row in candidates)
    chosen = [row for row in candidates if rank(row) == best]
    return chosen if len(chosen) == 1 else None


async def _recording_candidates(db, item, key, label):
    conditions = []
    if key:
        conditions.append(Work.match_key == key)
        if label:
            conditions.append(display_title_sql(Work.title) == label)
    rows = (
        (
            await db.scalars(
                select(Work).where(
                    Work.metadata_fields["identity_rejected"].astext.is_distinct_from("true"),
                    or_(*conditions),
                )
            )
        ).all()
        if conditions
        else []
    )
    recording = recording_kind(item.title, item.authors)
    rows = [
        candidate
        for candidate in rows
        if (
            library_key(candidate.title, candidate.authors) == key
            or (key and candidate.match_key == key)
            or _same_recording_label(item.title, item.authors, candidate.title, candidate.authors)
            or (
                recording
                and display_title(candidate.title) == label
                and _credited_author(item.authors, candidate.authors)
            )
        )
        and (
            not candidate.language
            or not item.language
            or catalog_language(candidate.language) == catalog_language(item.language)
        )
    ]
    by_root = {}
    for candidate in rows:
        root = await canonical_work(db, candidate.id)
        # A merged copy can appear before its survivor. Keep the survivor.
        stored = by_root.get(root.id)
        if stored is None or stored.redirect_to:
            by_root[root.id] = root
    return list(by_root.values())


def _library_copy(work):
    """A book Dewarr created from a library item, not one from a catalog or an admin."""
    fields = work.metadata_fields or {}
    return (
        work.provisional
        and not work.catalog_public
        and not work.redirect_to
        and fields.get("origin") == "audiobookshelf"
        and not fields.get("display_separate")
    )


def _shelf_duplicate(work, item, key):
    """A library-created copy of the same book, such as its (Unabridged) or dramatized title."""
    return _library_copy(work) and (
        library_key(work.title, work.authors) == key
        or work.match_key == key
        or _same_recording_label(item.title, item.authors, work.title, work.authors)
    )


def _choose(candidates):
    preferred = _prefer_catalog_book(candidates)
    if preferred:
        return preferred[0]
    # Library-created copies of one book: the first one created keeps its parts together.
    if all(_library_copy(row) for row in candidates):
        return min(candidates, key=lambda row: (row.created_at, str(row.id)))
    return None


def _relabel(work, item, key):
    """Key and title a library-created book by the book, not by one recording's labels."""
    if not _library_copy(work):
        return
    if key and work.match_key != key:
        work.match_key = key
    locked = (work.metadata_fields or {}).get("fields", {})
    base = parse_title_labels(item.title).title
    if (
        work.title == item.title
        and base != item.title
        and not locked.get("title", {}).get("locked")
    ):
        work.title = base
    kept = identity_authors(item.authors)[0]
    if (
        kept
        and work.authors == item.authors
        and kept != item.authors
        and not locked.get("authors", {}).get("locked")
    ):
        work.authors = kept


async def resolve_abs_work(db: AsyncSession, item: ABSItem, link: ProviderObject) -> Work | None:
    if link.manual_lock:
        return await db.get(Work, link.work_id) if link.work_id else None
    key = library_key(item.title, item.authors)
    label = display_title(parse_title_labels(item.title).title)
    if link.work_id:
        current = await db.get(Work, link.work_id)
        if not current:
            link.match_status = "needs-review"
            return None
        if current.redirect_to:
            current = await canonical_work(db, current.id)
        old = link.snapshot or {}
        if old and library_key(old.get("title", ""), old.get("authors", [])) != key:
            if not _same_recording_label(
                item.title, item.authors, old.get("title", ""), old.get("authors", [])
            ):
                link.match_status = "needs-review"
                return None
        if _shelf_duplicate(current, item, key):
            _relabel(current, item, key)
            others = [
                candidate
                for candidate in await _recording_candidates(db, item, key, label)
                if candidate.id != current.id
            ]
            chosen = _choose([current, *others]) if others else None
            if chosen and (
                (not chosen.provisional and chosen.catalog_public) or _library_copy(chosen)
            ):
                current = chosen
        link.work_id, link.match_status = current.id, "matched"
        return current
    candidates = await _recording_candidates(db, item, key, label)
    if len(candidates) > 1:
        chosen = _choose(candidates)
        if not chosen:
            link.match_status = "needs-review"
            return None
        candidates = [chosen]
    if candidates:
        work = candidates[0]
        _relabel(work, item, key)
    else:
        kept = identity_authors(item.authors)[0]
        work = Work(
            title=parse_title_labels(item.title).title,
            authors=kept or item.authors,
            description=item.description,
            language=item.language,
            provisional=True,
            catalog_public=False,
            match_key=key,
            metadata_fields={"origin": "audiobookshelf", "fields": {}},
        )
        db.add(work)
        await db.flush()
    link.work_id, link.match_status = work.id, "matched"
    return work


async def resolve_abs_version(
    db: AsyncSession, work: Work, item: ABSItem, medium: str, link: ProviderObject, *, part=...
) -> Version:
    """``part`` is (N, M) or None for a whole book; by default it is read from the title."""
    if link.version_id:
        version = await db.get(Version, link.version_id)
        if version and version.medium == medium and version.work_id:
            owner = await canonical_work(db, version.work_id)
            if owner.id == (await canonical_work(db, work.id)).id:
                return version
    # Unknown recording metadata cannot establish equivalence to another recording.
    version = None
    identifier = "asin" if medium == "audio" else "isbn"
    if not item.identifiers.get(identifier) and item.identifiers.get("hardcover"):
        identifier = "hardcover"
    if medium == "audio" and not item.identifiers.get(identifier) and item.identifiers.get("isbn"):
        identifier = "isbn"
    if item.identifiers.get(identifier):
        value = item.identifiers[identifier]
        stored = [Version.identifiers[identifier].astext == value]
        if identifier == "isbn":
            stored.extend(
                Version.identifiers[key].astext == value for key in ("isbn_13", "isbn_10")
            )
        candidates = (
            await db.scalars(
                select(Version).where(
                    Version.work_id == work.id,
                    Version.medium == medium,
                    or_(*stored),
                )
            )
        ).all()
        narrators = item.narrators if medium == "audio" else []
        candidates = [
            candidate
            for candidate in candidates
            if candidate.narrators == narrators
            and catalog_language(candidate.language) == catalog_language(item.language)
            and candidate.abridged == item.abridged
            and candidate.publication_year == item.year
        ]
        if len(candidates) == 1:
            version = candidates[0]
    kind = recording_kind(item.title, item.authors) if medium == "audio" else None
    part = item_part(item) if part is ... else part
    if not version and part and medium == "audio":
        # Another part of the same recording already has a version: share it.
        siblings = (
            await db.scalars(
                select(Version)
                .join(LibraryAsset, LibraryAsset.version_id == Version.id)
                .join(
                    AssetContains,
                    (AssetContains.asset_id == LibraryAsset.id)
                    & (AssetContains.work_id == work.id),
                )
                .where(
                    Version.work_id == work.id,
                    Version.medium == "audio",
                    Version.recording_kind.is_not_distinct_from(kind),
                    AssetContains.part_total == part[1],
                    AssetContains.part_index != part[0],
                )
                .distinct()
            )
        ).all()
        siblings = [
            candidate
            for candidate in siblings
            if catalog_language(candidate.language) == catalog_language(item.language)
            and candidate.abridged == item.abridged
        ]
        if len(siblings) == 1:
            version = siblings[0]
    if not version:
        version = Version(
            work_id=work.id,
            medium=medium,
            title=parse_title_labels(item.title).title if part else item.title,
            language=item.language,
            narrators=item.narrators if medium == "audio" else [],
            abridged=item.abridged,
            publication_year=item.year,
            # One part's identifiers do not identify the whole recording.
            identifiers={} if part else item.identifiers,
            recording_kind=kind,
        )
        db.add(version)
        await db.flush()
    link.version_id = version.id
    return version


def item_part(item) -> tuple[int, int] | None:
    """(N, M) when a library item holds part N of M of a book."""
    labels = parse_title_labels(item.title)
    if labels.part and labels.part_total and labels.part_total >= 2:
        return labels.part, labels.part_total
    return None


def version_changed(item: ABSItem, link: ProviderObject, medium: str) -> bool:
    """Changed recording/edition evidence requires review, even with a locked work match."""
    if not link.version_id or not link.snapshot:
        return False
    fields = ["language", "year", "abridged", "identifiers"]
    if medium == "audio":
        fields.append("narrators")
    # A field Dewarr could not read, now or at the last match, is missing evidence,
    # not a different recording.
    unread = set(item.read_issues) | set(link.snapshot.get("read_issues") or [])
    if unread & {"isbn", "asin"}:
        unread.add("identifiers")
    fields = [field for field in fields if field not in unread]
    current = item.model_dump(mode="json")
    return any(link.snapshot.get(field) != current.get(field) for field in fields)
