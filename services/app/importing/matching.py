"""Conservative local catalog matching for inspected book groups."""

from typing import Literal
from uuid import UUID

from sqlalchemy import exists, false, func, or_, select

from app.db.models import (
    Integration,
    Library,
    LibraryAsset,
    ProviderObject,
    Version,
    Work,
    WorkMetadataSource,
)
from app.domain.catalog_titles import display_title, display_title_sql, titles_agree
from app.domain.identity import normalized
from app.domain.work_graph import canonical_work
from app.importing.file_editions import FILE_EDITION_PROVIDER
from app.importing.match_evidence import (
    ISBN_KEYS,
    MatchEvidence,
    catalog_identifiers,
    group_evidence,
    isbn_forms,
    language_key,
)
from app.importing.naming import StrictModel, fingerprint
from app.importing.versioning import version_revision

MATCHER_VERSION = 3
MAX_CANDIDATES = 50


class MatchCandidate(StrictModel):
    work_id: UUID
    version_id: UUID
    title: str
    authors: list[str]
    version_title: str | None
    medium: str
    narrators: list[str]
    language: str | None
    year: int | None
    identifier_match: bool
    reasons: list[str]
    conflicts: list[str]
    version_revision: str
    work_revision: str


class GroupMatch(StrictModel):
    group_key: str
    status: Literal["matched", "review", "unmatched"]
    message: str
    evidence: MatchEvidence
    candidates: list[MatchCandidate]
    selected_version_id: UUID | None = None
    truncated: bool = False
    revision: str


class MatchPage(StrictModel):
    inspection_revision: str
    grouping_revision: str
    items: list[GroupMatch]
    total: int
    offset: int
    limit: int
    matcher_version: int = MATCHER_VERSION


def usable_version():
    # This endpoint is administrator-only, but withdrawn metadata and unavailable
    # inventory must not become a fresh automatic assertion.
    catalog = exists(
        select(ProviderObject.id)
        .join(WorkMetadataSource)
        .where(
            ProviderObject.version_id == Version.id,
            ProviderObject.kind == "edition",
            WorkMetadataSource.accepted.is_(True),
            WorkMetadataSource.provider.in_(["hardcover", "openlibrary"]),
        )
    )
    library = exists(
        select(LibraryAsset.id)
        .join(Library)
        .join(Integration)
        .where(
            LibraryAsset.version_id == Version.id,
            Library.accessible.is_(True),
            Integration.enabled.is_(True),
            LibraryAsset.match_status == "matched",
        )
    )
    # An edition created from a reviewed file is local evidence for the next import.
    recorded = exists(
        select(ProviderObject.id).where(
            ProviderObject.version_id == Version.id,
            ProviderObject.provider == FILE_EDITION_PROVIDER,
            ProviderObject.kind == "edition",
            ProviderObject.match_status == "matched",
        )
    )
    return or_(catalog, library, recorded)


def candidate_evidence(facts, version, origin, work, needs_review):
    expected_ids = {(item.namespace, item.value) for item in facts.identifiers}
    ids = catalog_identifiers(version.identifiers)
    shared = expected_ids & ids
    conflicts, reasons = [], []
    if shared:
        reasons.append("Embedded edition identifier matches")
    if expected_ids and not expected_ids <= ids:
        conflicts.append("Catalog identifiers do not support all embedded assertions")
    if titles_agree(facts.titles, (origin.title, work.title, version.title)):
        reasons.append("Embedded title agrees")
    else:
        conflicts.append("Embedded title is missing or differs")
    credits = {
        tuple(sorted(normalized(author) for author in entry.authors))
        for entry in (origin, work)
        if entry.authors
    }
    if facts.authors and all(tuple(names) in credits for names in facts.authors):
        reasons.append("Embedded authors agree")
    else:
        conflicts.append("Embedded authors are missing or differ")
    language = version.language or origin.language or work.language
    if (
        facts.languages
        and language
        and any(value != language_key(language) for value in facts.languages)
    ):
        conflicts.append("Language differs")
    if facts.years and version.publication_year and facts.years != [version.publication_year]:
        conflicts.append("Recording year differs")
    if facts.abridged and (version.abridged is None or facts.abridged != [version.abridged]):
        conflicts.append("Abridgment is unknown or differs")
    if version.medium == "audio":
        credited = sorted(normalized(name) for name in version.narrators)
        if not credited or facts.narrators != [credited]:
            conflicts.append("Narrator evidence is missing or differs")
        else:
            reasons.append("Narrator agrees")
    if needs_review:
        conflicts.append("Resolve this catalog version's pending metadata conflict")
    if origin.metadata_fields.get("identity_rejected") or work.metadata_fields.get(
        "identity_rejected"
    ):
        conflicts.append("This catalog identity was explicitly rejected")
    return MatchCandidate(
        work_id=work.id,
        version_id=version.id,
        title=work.title,
        authors=work.authors,
        version_title=version.title,
        medium=version.medium,
        narrators=version.narrators,
        language=version.language,
        year=version.publication_year,
        identifier_match=bool(shared),
        reasons=reasons,
        conflicts=conflicts,
        version_revision=version_revision(version),
        work_revision=fingerprint(
            {
                "origin": str(origin.id),
                "work": str(work.id),
                "title": work.title,
                "origin_title": origin.title,
                "authors": work.authors,
                "origin_authors": origin.authors,
                "language": work.language,
                "origin_language": origin.language,
                "rejected": bool(
                    origin.metadata_fields.get("identity_rejected")
                    or work.metadata_fields.get("identity_rejected")
                ),
            }
        ),
    )


async def match_group(db, snapshot, grouping_revision, group):
    facts = group_evidence(snapshot, group)
    conditions = []
    for item in facts.identifiers:
        scheme, value = item.namespace, item.value
        if scheme == "isbn":
            for key in ISBN_KEYS:
                conditions.append(
                    func.regexp_replace(
                        func.upper(
                            func.regexp_replace(
                                Version.identifiers[key].astext,
                                r"^(urn:)?isbn([-_ ]?(10|13))?[[:space:]]*:[[:space:]]*",
                                "",
                                "i",
                            )
                        ),
                        "[^0-9X]",
                        "",
                        "g",
                    ).in_(isbn_forms(value))
                )
        elif scheme == "asin":
            conditions.append(func.upper(Version.identifiers["asin"].astext) == value)
    # Put every exact identifier candidate before title-only suggestions. A
    # display limit must not hide an edition or manufacture uniqueness.
    identifier_condition = or_(*conditions) if conditions else false()
    identifier_order = [identifier_condition.desc().nulls_last()] if conditions else []
    stripped = sorted({display_title(title) for title in facts.titles if display_title(title)})
    if stripped:
        conditions.extend(
            [
                display_title_sql(Work.title).in_(stripped),
                display_title_sql(Version.title).in_(stripped),
            ]
        )
    needs_review = exists(
        select(ProviderObject.id).where(
            ProviderObject.version_id == Version.id,
            ProviderObject.match_status == "needs-review",
        )
    )
    rows = (
        (
            await db.execute(
                select(Version, Work, needs_review, identifier_condition)
                .join(Work)
                .where(
                    Version.medium == group.medium,
                    usable_version(),
                    or_(*conditions),
                )
                .order_by(*identifier_order, Version.id)
                .limit(MAX_CANDIDATES + 1)
                .execution_options(populate_existing=True)
            )
        ).all()
        if conditions
        else []
    )
    candidates = []
    for version, origin, review, _ in rows[:MAX_CANDIDATES]:
        work = await canonical_work(db, origin.id)
        candidates.append(candidate_evidence(facts, version, origin, work, review))
    candidates.sort(
        key=lambda row: (not row.identifier_match, len(row.conflicts), str(row.version_id))
    )
    identified = [row for row in candidates if row.identifier_match]
    chosen = (
        identified[0]
        if len(identified) == 1
        and not identified[0].conflicts
        and not facts.issues
        and not (len(rows) > MAX_CANDIDATES and rows[MAX_CANDIDATES][3])
        else None
    )
    status = "matched" if chosen else "review" if candidates or facts.issues else "unmatched"
    if chosen:
        message = "One catalog edition agrees with the embedded identity evidence"
    elif len(rows) > MAX_CANDIDATES:
        message = "Too many possible versions; narrow the match manually"
    elif len(identified) > 1:
        message = "The identifier appears on multiple catalog versions; review the match"
    elif facts.issues:
        message = facts.issues[0]
    elif candidates:
        message = (
            "Possible catalog matches need review; title similarity is not an edition identifier"
        )
    else:
        message = (
            "No catalog edition of this format matches. "
            "Search for the book and add an edition from the file"
        )
    content = {
        "group_key": group.key,
        "status": status,
        "message": message,
        "evidence": facts.model_dump(),
        "candidates": [row.model_dump(mode="json") for row in candidates],
        "selected_version_id": str(chosen.version_id) if chosen else None,
        "truncated": len(rows) > MAX_CANDIDATES,
    }
    return GroupMatch(
        **content,
        revision=fingerprint(
            {
                "matcher": MATCHER_VERSION,
                "inspection": snapshot["revision"],
                "grouping": grouping_revision,
                "result": content,
            }
        ),
    )
