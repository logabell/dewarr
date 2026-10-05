"""Explicit trailing labels in indexer release names, never arbitrary title words."""

import re

from app.domain.catalog_language import LANGUAGE_ALIASES

# Full names avoid mistaking short language codes for initials or title words.
LANGUAGES = {name: code for name, code in LANGUAGE_ALIASES.items() if len(name) > 3}
LANGUAGES["deutsch"] = "de"
MEDIA = frozenset({"audiobook", "abook", "hoerbuch", "hörbuch", "hoerspiel", "hörspiel"})
FORMATS = frozenset({"m4b", "mp3", "epub", "pdf", "flac", "aac", "ogg", "opus", "azw3", "mobi"})
LABELS = MEDIA | FORMATS | LANGUAGES.keys() | {"unabridged", "ungekuerzt", "ungekürzt", "retail"}
_LABEL = rf"(?:{'|'.join(sorted(LABELS))}|(?:19|20)\d{{2}})"
_SUFFIX = re.compile(
    rf"(?:[.\s_]*\[\s*(?P<square>{_LABEL})\s*\]|"
    rf"[.\s_]*\(\s*(?P<round>{_LABEL})\s*\)|"
    rf"[.\s_]+(?P<bare>{_LABEL}))\s*$",
    re.I,
)
_GROUP = re.compile(r"(?i:(?:\.|\s)(?:m4b|mp3|flac|aac|ogg|opus))-(?P<group>[A-Z0-9]{2,16})$")


def indexer_suffixes(value):
    """Yield progressively unlabelled titles, retaining the original as a candidate.

    Callers match the catalog pair against every candidate, so actual titles such
    as 'German' or 'Unabridged' are never lost through unconditional stripping.
    """
    labels = []
    yield value, labels
    if group := _GROUP.search(value):
        # A scene group after an audio extension is metadata. Partial/preview
        # markers are content warnings even if they use the same spelling.
        if not re.fullmatch(r"SAMPLE|PREVIEW|SUMMARY|PART\d*|CD\d*|DISC\d*", group["group"]):
            value = value[: group.start("group") - 1]
    while match := _SUFFIX.search(value):
        prefix = value[: match.start()].rstrip()
        if not prefix:
            break
        label = next(part for part in match.groups() if part is not None).casefold()
        explicit = match["bare"] is None
        labels = [*labels, (label, explicit)]
        value = prefix
        yield value, labels


def label_language(labels, *, catalog_boundary=False):
    languages = {LANGUAGES[label] for label, _ in labels if label in LANGUAGES}
    if len(languages) != 1:
        return None
    if any(label in LANGUAGES and explicit for label, explicit in labels) or (
        catalog_boundary and any(label in MEDIA | FORMATS for label, _ in labels)
    ):
        return languages.pop()
    return None


def indexer_language(value):
    """Without a catalog boundary, only bracketed language claims are explicit."""
    return label_language(list(indexer_suffixes(value))[-1][1])
