"""Use the saved book association for one complete, unambiguous download."""

from uuid import UUID

from app.domain.catalog_titles import display_title, optional_subtitle_base
from app.domain.identity import normalized
from app.domain.work_graph import canonical_work
from app.importing.file_editions import attach_file_edition
from app.importing.match_evidence import group_evidence, language_key


def agrees_with_request(work, release, facts):
    # Missing tags are common in M4B files. Conflicting tags still require review.
    if facts.issues or work.metadata_fields.get("identity_rejected"):
        return False
    title = display_title(work.title)
    titles = {title, display_title(optional_subtitle_base(work.title))}
    authors = sorted(normalized(name) for name in work.authors)
    if not title or not authors or display_title(release.get("title", "")) not in titles:
        return False
    if any(display_title(value) not in titles for value in facts.titles):
        return False
    if any(value != authors for value in facts.authors):
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
    if not agrees_with_request(work, selection.frozen["release"], facts):
        return None
    version, _ = await attach_file_edition(
        db, approver, inspection.id, work.id, group.key, grouping_revision
    )
    return version
