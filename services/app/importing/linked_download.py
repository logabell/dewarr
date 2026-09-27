"""Use the saved book association for one complete, unambiguous download."""

from types import SimpleNamespace
from uuid import UUID

from app.domain.book_sources import identity
from app.domain.catalog_titles import display_title, optional_subtitle_base
from app.domain.identity import normalized
from app.domain.release_profiles import indexer_title_authors
from app.domain.series_identity import title_outside_series_note
from app.domain.work_graph import canonical_work
from app.importing.file_editions import attach_file_edition
from app.importing.match_evidence import group_evidence, language_key


def agrees_with_request(work, release, facts, *, series=()):
    # Missing tags are common in M4B files. Conflicting tags still require review.
    if facts.issues or work.metadata_fields.get("identity_rejected"):
        return False
    title = display_title(work.title)
    titles = {title, display_title(optional_subtitle_base(work.title))}
    authors = sorted(normalized(name) for name in work.authors)
    if not title or not authors:
        return False
    catalog = {"title": work.title, "authors": work.authors, "series": list(series)}
    credited_source = release.get("source") in {"audiobookbay", "prowlarr"}
    file_titles_agree = bool(facts.titles) and all(
        display_title(value) in titles
        or (credited_source and title_outside_series_note(value, catalog) is not None)
        for value in facts.titles
    )
    file_authors_cover = bool(facts.authors) and all(
        set(authors) <= set(value) for value in facts.authors
    )
    file_corroborated = file_titles_agree and file_authors_cover
    shown = display_title(release.get("title", ""))
    # New credit/series forms need independent file tags, and the release must
    # still name this book or its exact known series position.
    credited_release = (
        credited_source
        and file_corroborated
        and bool(
            indexer_title_authors(
                SimpleNamespace(
                    source=release["source"],
                    title=release.get("title", ""),
                    raw_title=release.get("raw_title") or release.get("title", ""),
                    authors=[],
                ),
                catalog,
            )
        )
    )
    if shown not in titles and not credited_release:
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
    if not release_authors and not facts.authors:
        return False
    if work.language and any(value != language_key(work.language) for value in facts.languages):
        return False
    return True


async def linked_version(db, approver, selection, inspection, group, grouping_revision):
    # Specific-edition requests must retain their stronger edition evidence.
    rule = selection.frozen["requirements"]
    if rule.get("version_id") or rule["medium"] != group.medium:
        return None
    work = await canonical_work(db, UUID(selection.frozen["origin_work_id"]))
    facts = group_evidence(inspection.snapshot, group)
    catalog = await identity(db, work, selection.owner_id)
    if not agrees_with_request(work, selection.frozen["release"], facts, series=catalog["series"]):
        return None
    version, _ = await attach_file_edition(
        db, approver, inspection.id, work.id, group.key, grouping_revision
    )
    return version
