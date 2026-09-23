import hashlib
import json
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.audiobookshelf import ABSItem
from app.db.models import ProviderObject, Version, Work
from app.domain.catalog_language import catalog_language
from app.domain.catalog_titles import display_title, display_title_sql
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


def _same_recording_label(title, authors, other_title, other_authors):
    label = display_title(title)
    # Title alone cannot identify a work, with or without an edition label.
    if not label or not authors or not other_authors:
        return False
    return label == display_title(other_title) and work_key(label, authors) == work_key(
        label, other_authors
    )


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
    rows = [
        candidate
        for candidate in rows
        if (
            work_key(candidate.title, candidate.authors) == key
            or _same_recording_label(item.title, item.authors, candidate.title, candidate.authors)
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


def _shelf_duplicate(work, item):
    """An Audiobookshelf-created copy whose title only adds an edition label."""
    fields = work.metadata_fields or {}
    return (
        work.provisional
        and not work.catalog_public
        and not work.redirect_to
        and fields.get("origin") == "audiobookshelf"
        and not fields.get("display_separate")
        and _same_recording_label(item.title, item.authors, work.title, work.authors)
    )


async def resolve_abs_work(db: AsyncSession, item: ABSItem, link: ProviderObject) -> Work | None:
    if link.manual_lock:
        return await db.get(Work, link.work_id) if link.work_id else None
    key = work_key(item.title, item.authors)
    label = display_title(item.title)
    if link.work_id:
        current = await db.get(Work, link.work_id)
        if not current:
            link.match_status = "needs-review"
            return None
        if current.redirect_to:
            current = await canonical_work(db, current.id)
        old = link.snapshot or {}
        if old and work_key(old.get("title", ""), old.get("authors", [])) != key:
            if not _same_recording_label(
                item.title, item.authors, old.get("title", ""), old.get("authors", [])
            ):
                link.match_status = "needs-review"
                return None
        if _shelf_duplicate(current, item):
            others = [
                candidate
                for candidate in await _recording_candidates(db, item, key, label)
                if candidate.id != current.id
            ]
            preferred = _prefer_catalog_book(others) if others else None
            if preferred and not preferred[0].provisional and preferred[0].catalog_public:
                current = preferred[0]
        link.work_id, link.match_status = current.id, "matched"
        return current
    candidates = await _recording_candidates(db, item, key, label)
    if len(candidates) > 1:
        preferred = _prefer_catalog_book(candidates)
        if not preferred:
            link.match_status = "needs-review"
            return None
        candidates = preferred
    if candidates:
        work = candidates[0]
    else:
        work = Work(
            title=item.title,
            authors=item.authors,
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
    db: AsyncSession, work: Work, item: ABSItem, medium: str, link: ProviderObject
) -> Version:
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
    if not version:
        version = Version(
            work_id=work.id,
            medium=medium,
            title=item.title,
            language=item.language,
            narrators=item.narrators if medium == "audio" else [],
            abridged=item.abridged,
            publication_year=item.year,
            identifiers=item.identifiers,
        )
        db.add(version)
        await db.flush()
    link.version_id = version.id
    return version


def version_changed(item: ABSItem, link: ProviderObject, medium: str) -> bool:
    """Changed recording/edition evidence requires review, even with a locked work match."""
    if not link.version_id or not link.snapshot:
        return False
    fields = ["language", "year", "abridged", "identifiers"]
    if medium == "audio":
        fields.append("narrators")
    # A field Dewarr could not read is missing evidence, not a different recording.
    unread = set(item.read_issues)
    if unread & {"isbn", "asin"}:
        unread.add("identifiers")
    fields = [field for field in fields if field not in unread]
    current = item.model_dump(mode="json")
    return any(link.snapshot.get(field) != current.get(field) for field in fields)
