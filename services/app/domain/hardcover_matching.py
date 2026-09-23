"""Evidence-based Hardcover work matching; edition ownership is never inferred."""

import re
import unicodedata

from pydantic import BaseModel, Field

from app.adapters.catalog_types import BookData
from app.adapters.contracts import AdapterError, FailureKind
from app.domain.catalog_language import catalog_language
from app.domain.catalog_titles import (
    display_title,
    distinct_work_subtitle,
    identity_authors,
    parse_title_labels,
)
from app.importing.match_evidence import catalog_identifiers


class MatchEvidence(BaseModel):
    title: str
    authors: list[str]
    language: str | None = None
    identifiers: list[tuple[str, str]] = Field(default_factory=list)
    # Series name and position the library reports, such as ("Red Rising Saga", "5").
    series: list[tuple[str, str | None]] = Field(default_factory=list)


class MatchResult(BaseModel):
    book: BookData | None = None
    candidates: list[BookData] = Field(default_factory=list)
    status: str = "unmatched"
    basis: str | None = None
    reason: str = "No confident Hardcover match was found."


def words(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(
        re.findall(r"[^\W_]+", "".join(c for c in value if not unicodedata.combining(c)))
    )


def authors_key(values):
    return {words(value).replace(" ", "") for value in values if words(value)}


SERIES_SUFFIX = re.compile(r"\s*\(([^()#]+?),?\s+#(\d+(?:\.\d+)?)\)\s*$")


def matching_title(value):
    # A single Goodreads series membership is not part of the book title.
    # Ranges/sets and unnumbered parentheses remain identity-bearing text.
    return SERIES_SUFFIX.sub("", value).strip()


def title_parts(value):
    # Only known edition labels are removed. Volume numbers, adaptations, and
    # arbitrary subtitles are not silently erased.
    value = display_title(matching_title(value))
    parts = re.split(r":\s+", value, maxsplit=1)
    return words(parts[0]), words(parts[1]) if len(parts) > 1 else ""


# Different content: never the book itself, whatever either side's labels say.
DERIVATIVE = re.compile(r"\b(?:summary|study guide|graphic novel|box set|omnibus)\b", re.I)
# A catalog entry for a recording rather than the book. A library recording with these
# labels is an edition of the book, so it matches the book and never these entries.
RECORDING = re.compile(
    r"\b(?:\d+\s+of\s+\d+|dramati[sz](?:ed|ation)|full[ -]cast|adaptation)\b", re.I
)
_SERIES_FILLER = {"the", "a", "an", "series", "saga", "trilogy", "cycle", "sequence", "novels"}


def series_name(value):
    return " ".join(part for part in words(value).split() if part not in _SERIES_FILLER)


def _position(value):
    try:
        return float(str(value).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None


def series_agrees(evidence_series, book_series):
    """Some library series names the catalog series at the same position."""
    for name, sequence in evidence_series:
        for entry in book_series:
            if series_name(name) and series_name(name) == series_name(entry.name):
                if sequence is None or _position(sequence) == _position(entry.position):
                    return True
    return False


def series_conflict(evidence_series, book_series):
    """The same series at a different position is a different book."""
    for name, sequence in evidence_series:
        for entry in book_series:
            if (
                series_name(name)
                and series_name(name) == series_name(entry.name)
                and sequence is not None
                and _position(sequence) is not None
                and _position(entry.position) is not None
                and _position(sequence) != _position(entry.position)
            ):
                return True
    return False


def recording_entry(book):
    """A catalog book that is itself a dramatization or one part of a recording."""
    labels = parse_title_labels(book.title)
    return bool(labels.recording_kind or labels.part or RECORDING.search(book.title))


def compatible(evidence, book, *, identified=False, series_title=False):
    """Whether a catalog book is the book the evidence describes.

    ``series_title`` means the evidence title had a series prefix removed and has no
    author, so the caller must confirm the catalog series before accepting it.
    """
    title = parse_title_labels(evidence.title).title
    if RECORDING.search(title) or recording_entry(book):
        return False
    if DERIVATIVE.search(title + " " + book.title):
        return False
    left_series = SERIES_SUFFIX.search(title)
    right_series = SERIES_SUFFIX.search(book.title)
    if (
        left_series
        and right_series
        and (
            words(left_series[1].rstrip(",")) != words(right_series[1].rstrip(","))
            or left_series[2] != right_series[2]
        )
    ):
        return False
    if (distinct_work_subtitle(title) or distinct_work_subtitle(book.title)) and display_title(
        title
    ) != display_title(book.title):
        return False
    left, right = title_parts(title), title_parts(book.title)
    # A missing subtitle colon is punctuation, not a different title.
    complete_title_equal = words(display_title(matching_title(title))) == words(
        display_title(matching_title(book.title))
    )
    if not complete_title_equal and (
        left[0] != right[0] or (left[1] and right[1] and left[1] != right[1])
    ):
        return False
    if evidence.series and book.series and series_conflict(evidence.series, book.series):
        return False
    a, b = authors_key(identity_authors(evidence.authors)[0]), authors_key(book.authors)
    if not a and series_title:
        # Only a producer credit: the caller confirms the catalog series instead.
        return bool(b)
    # Identifier hits can share one author. A title search can include an
    # illustrator or other credit the catalog does not treat as an author.
    # Extra catalog authors stay distinct: that can be a different book.
    if not a or not b or (not (a & b) if identified else not b <= a):
        return False
    language = catalog_language(evidence.language)
    if language and book.language and language != catalog_language(book.language):
        return False
    if language and book.editions and not identified:
        languages = {
            catalog_language(edition.language) for edition in book.editions if edition.language
        }
        if languages and language not in languages:
            return False
    return True


async def lookup(evidence, call):
    async def canonical(book):
        seen = set()
        for _ in range(4):
            key = book.canonical_id or book.external_id
            if key in seen:
                return None
            seen.add(key)
            book, stale, _ = await call("fetch", key)
            if stale or book.external_id != key:
                return None
            if book.canonical_id in (None, book.external_id):
                return book
        return None

    async def resolve(candidates):
        """Full canonical records, fetched in one request when there are several."""
        if len(candidates) < 2:
            return [await canonical(candidate) for candidate in candidates]
        keys = list(dict.fromkeys(c.canonical_id or c.external_id for c in candidates))
        fetched, stale, _ = await call("fetch_many", keys)
        if stale:
            return [None] * len(candidates)
        books = []
        for candidate in candidates:
            book = fetched.get(candidate.canonical_id or candidate.external_id)
            if book and book.canonical_id not in (None, book.external_id):
                book = await canonical(book)
            books.append(book)
        return books

    if evidence.identifiers:
        page, stale, _ = await call("identifier_search", evidence.identifiers)
        if stale or page.has_more:
            return MatchResult(
                reason="Identifier results were incomplete or stale; review the match."
            )
        recordings = False
        if page.items:
            matches = {}
            pending = []
            for candidate in page.items:
                candidate_ids = set().union(
                    *(catalog_identifiers(e.identifiers) for e in candidate.editions)
                )
                if not candidate_ids.intersection(evidence.identifiers):
                    return MatchResult(
                        reason="Hardcover returned an edition without matching identifiers."
                    )
                # A canonical search hit already has the title and authors to check,
                # so a rejected hit costs no full fetch.
                if candidate.canonical_id is None and recording_entry(candidate):
                    recordings = True
                    continue
                if candidate.canonical_id is None and not compatible(
                    evidence, candidate, identified=True
                ):
                    return MatchResult(
                        reason="Identifiers conflict with the title or author; review the match."
                    )
                pending.append(candidate)
            for candidate, book in zip(pending, await resolve(pending), strict=True):
                if book and recording_entry(book):
                    # The identifier names Hardcover's entry for this recording. The book
                    # it records is found by title below.
                    recordings = True
                    continue
                if not book or not compatible(evidence, book, identified=True):
                    return MatchResult(
                        reason="Identifiers conflict with the title or author; review the match."
                    )
                language = catalog_language(evidence.language)
                matching_editions = [
                    e
                    for e in candidate.editions
                    if catalog_identifiers(e.identifiers).intersection(evidence.identifiers)
                ]
                if language and any(
                    e.language and catalog_language(e.language) != language
                    for e in matching_editions
                ):
                    return MatchResult(
                        reason="The identified edition has a different language; review the match."
                    )
                if candidate.external_id == book.external_id:
                    known = {edition.external_id for edition in book.editions}
                    book.editions.extend(
                        edition for edition in matching_editions if edition.external_id not in known
                    )
                matches[book.external_id] = book
            if len(matches) == 1:
                return MatchResult(
                    book=next(iter(matches.values())),
                    status="matched",
                    basis="identifier",
                    reason=(
                        "Verified ISBN/ASIN with compatible title and author, "
                        "resolved to the current Hardcover book."
                    ),
                )
            if matches or not recordings:
                return MatchResult(
                    reason="The supplied identifiers point to different books; review the match."
                )
    labels = parse_title_labels(evidence.title)
    authors = identity_authors(evidence.authors)[0]
    attempts = [(labels.title, None)] if authors else []
    if labels.series_title and (labels.series or labels.sequence):
        # "Mistborn 2 - The Well of Ascension": the catalog must list that series position.
        attempts.append((labels.series_title, (labels.series, labels.sequence)))
    if not attempts:
        return MatchResult(
            reason="An author or verified identifier is needed to identify this book."
        )
    result = None
    for title, prefix in attempts:
        result = await _title_lookup(evidence, title, authors, prefix, resolve, call)
        if result.status == "matched":
            return result
    return result


def _prefix_agrees(prefix, book):
    name, sequence = prefix
    if name:
        return series_agrees([(name, sequence)], book.series)
    return any(_position(entry.position) == _position(sequence) for entry in book.series)


async def _title_lookup(evidence, title, authors, prefix, resolve, call):
    evidence = evidence.model_copy(update={"title": title})
    search_title = display_title(matching_title(title)).split(":", 1)[0]
    if authors:
        try:
            page, stale, _ = await call("search", f"{search_title} {authors[0]}", 1, None)
        except AdapterError as error:
            if error.kind != FailureKind.PARSER:
                raise
            # Malformed unrelated search hits must not prevent a bounded title lookup.
            page, stale, _ = await call("title_search", search_title)
        else:
            if not stale and page.has_more:
                page, stale, _ = await call("title_search", search_title)
    else:
        page, stale, _ = await call("title_search", search_title)
    if stale or page.has_more:
        return MatchResult(reason="Search results are incomplete; a unique match needs review.")
    from_series = prefix is not None and not authors
    candidates = [
        item for item in page.items if compatible(evidence, item, series_title=from_series)
    ]
    # A bounded search never fetches every near-match from a broad result set.
    if not candidates or len(candidates) > 4:
        return MatchResult(
            candidates=[item for item in page.items if not recording_entry(item)][:8],
            reason="No unique title-and-author match was found. Review the catalog candidates.",
        )
    matches = {}
    for book in await resolve(candidates):
        if not book or not compatible(evidence, book, series_title=from_series):
            return MatchResult(
                reason="The provider’s full book details conflict with the search result."
            )
        if prefix is not None and not _prefix_agrees(prefix, book):
            continue
        matches[book.external_id] = book
    if len(matches) > 1 and evidence.series:
        agreeing = {
            key: book
            for key, book in matches.items()
            if series_agrees(evidence.series, book.series)
        }
        if len(agreeing) == 1:
            matches = agreeing
    if len(matches) != 1:
        return MatchResult(
            candidates=list(matches.values()),
            reason=(
                "More than one Hardcover book fits the title and author; review the match."
                if matches
                else "No Hardcover book lists this series position. Review the catalog candidates."
            ),
        )
    return MatchResult(
        book=next(iter(matches.values())),
        status="matched",
        basis="title-series" if from_series else "title-author",
        reason=(
            "Verified the title and catalog series position against the full Hardcover record."
            if from_series
            else "Verified a unique normalized title and author against the full Hardcover book "
            "record."
        ),
    )
