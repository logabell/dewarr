"""Read-only matching of Goodreads collection identities to Hardcover records."""

from app.domain.hardcover_matching import MatchEvidence, matching_title
from app.importing.match_evidence import catalog_identifiers


def evidence_for(entry):
    # Goodreads appends series membership to titles. Only remove a single numbered
    # membership label; ranges, boxed sets, and meaningful subtitles remain intact.
    title = matching_title(entry.title)
    return MatchEvidence(
        title=title,
        authors=entry.authors,
        identifiers=sorted(catalog_identifiers(entry.identifiers)),
    )


async def resolve_entry(entry, call):
    """Resolve presentation only; never persist a catalog/ownership binding.

    Uses the same rules as library matching, so a book that matches in one place
    matches everywhere. Conflicting candidates remain choices.
    """
    from app.domain.hardcover_matching import lookup

    return await lookup(evidence_for(entry), call)
