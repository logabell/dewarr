"""Use the saved book association for one complete, unambiguous download."""

from types import SimpleNamespace
from uuid import UUID

from app.db.models import Version
from app.domain.book_sources import identity
from app.domain.catalog_titles import display_title
from app.domain.identity import normalized
from app.domain.release_profiles import indexer_title_authors
from app.domain.series_identity import title_outside_series_note
from app.domain.title_matching import compatible_title
from app.domain.work_graph import canonical_work
from app.importing.file_editions import attach_file_edition
from app.importing.match_evidence import group_evidence, language_key


def request_file_conflicts(work, release, facts, *, series=(), reviewed_collection=False):
    """Absent tags are neutral; contradictory tags need a different download or correction."""
    catalog = {"title": work.title, "authors": work.authors, "series": list(series)}
    credited = release.get("source") in {"audiobookbay", "prowlarr"}
    title_agrees = bool(facts.titles) and all(
        compatible_title(value, work.title, allow_extra_subtitle=reviewed_collection)
        or (title_outside_series_note(value, catalog) is not None)
        for value in facts.titles
    )
    authors = sorted(normalized(name) for name in work.authors)
    conflicts = list(facts.issues)
    if facts.titles and not title_agrees:
        conflicts.append("The files name a different book")
    if any(
        names != authors and not (credited and title_agrees and set(authors) <= set(names))
        for names in facts.authors
    ):
        conflicts.append("The files name a different author")
    if work.language and any(value != language_key(work.language) for value in facts.languages):
        conflicts.append("The files use a different language")
    return conflicts


def agrees_with_request(work, release, facts, *, series=()):
    # Missing tags are common in M4B files. Conflicting tags still require review.
    if request_file_conflicts(work, release, facts, series=series) or work.metadata_fields.get(
        "identity_rejected"
    ):
        return False
    title = display_title(work.title)
    authors = sorted(normalized(name) for name in work.authors)
    if not title or not authors:
        return False
    catalog = {"title": work.title, "authors": work.authors, "series": list(series)}
    credited_source = release.get("source") in {"audiobookbay", "prowlarr"}
    file_titles_agree = bool(facts.titles) and all(
        compatible_title(value, work.title)
        or (title_outside_series_note(value, catalog) is not None)
        for value in facts.titles
    )
    file_authors_cover = bool(facts.authors) and all(
        set(authors) <= set(value) for value in facts.authors
    )
    file_corroborated = file_titles_agree and file_authors_cover
    shown = display_title(release.get("title", ""))
    # Proxied releases with an exact catalog author/title pair can identify an
    # otherwise untagged download. The caller has verified the complete manifest.
    # ABB series/credit forms still need independent file corroboration.
    credited_release = (
        credited_source
        and (file_corroborated or release.get("source") == "prowlarr")
        and bool(
            indexer_title_authors(
                SimpleNamespace(
                    source=release["source"],
                    title=release.get("title", ""),
                    raw_title=release.get("raw_title") or release.get("title", ""),
                    authors=[],
                ),
                catalog if file_corroborated else {**catalog, "series": []},
            )
        )
    )
    if not compatible_title(shown, work.title) and not credited_release:
        return False
    if facts.titles and not file_titles_agree:
        return False
    if any(
        value != authors and not (credited_source and file_corroborated) for value in facts.authors
    ):
        return False
    release_authors = sorted(normalized(name) for name in release.get("authors", []))
    if release_authors and release_authors != authors:
        return False
    if not release_authors and not facts.authors and not credited_release:
        return False
    if work.language and any(value != language_key(work.language) for value in facts.languages):
        return False
    return True


async def linked_version(db, approver, selection, inspection, group, grouping_revision, *, match):
    # Specific-edition requests must retain their stronger edition evidence.
    rule = selection.frozen["requirements"]
    if rule.get("version_id") or rule["medium"] != group.medium:
        return None
    work = await canonical_work(db, UUID(selection.frozen["origin_work_id"]))
    facts = group_evidence(inspection.snapshot, group)
    catalog = await identity(db, work, selection.owner_id)
    if not agrees_with_request(work, selection.frozen["release"], facts, series=catalog["series"]):
        return None
    identified = [candidate for candidate in match.candidates if candidate.identifier_match]
    if identified:
        # Missing tags do not undo the saved book selection. Keep the edition
        # when its identifier uniquely belongs to that book, but never erase
        # contradictory tags, another work, or an ambiguous identifier.
        missing = set()
        if not facts.titles:
            missing.add("Embedded title is missing or differs")
        if not facts.authors:
            missing.add("Embedded authors are missing or differ")
        if (
            len(identified) != 1
            or identified[0].work_id != work.id
            or set(identified[0].conflicts) - missing
        ):
            return None
        return await db.get(Version, identified[0].version_id)
    version, _ = await attach_file_edition(
        db, approver, inspection.id, work.id, group.key, grouping_revision
    )
    return version


async def linked_collection_version(
    db, approver, members, inspection, group, grouping_revision, match
):
    """Per-book file evidence must corroborate a reviewed collection mapping."""
    from pathlib import PurePosixPath

    group_paths = {f.path for f in group.files}
    eligible = []
    for member in members:
        review = member.frozen.get("collection_review")
        if not review:
            continue
        root = member.frozen["descriptor"]["name"]
        paths = {
            str(PurePosixPath(p).relative_to(root)) if p.startswith(root + "/") else p
            for p in review["paths"]
        }
        if paths == group_paths:
            eligible.append(member)
    if len(eligible) != 1:
        return None
    member = eligible[0]
    if (
        member.frozen["requirements"].get("version_id")
        or member.frozen["requirements"]["medium"] != group.medium
    ):
        return None
    work = await canonical_work(db, UUID(member.frozen["origin_work_id"]))
    facts = group_evidence(inspection.snapshot, group)
    # A filename or uploader claim cannot supply missing embedded identity here.
    if (
        not facts.titles
        or not facts.authors
        or work.metadata_fields.get("identity_rejected")
        or request_file_conflicts(work, member.frozen["release"], facts, reviewed_collection=True)
    ):
        return None
    identified = [c for c in match.candidates if c.identifier_match]
    if identified:
        if len(identified) != 1 or identified[0].work_id != work.id or identified[0].conflicts:
            return None
        return await db.get(Version, identified[0].version_id)
    version, _ = await attach_file_edition(
        db, approver, inspection.id, work.id, group.key, grouping_revision
    )
    return version
