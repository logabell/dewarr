"""Normalize presentation-only title labels without discarding actual subtitles."""

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import func, literal, literal_column

# A repeated trailing chain makes the order of edition labels irrelevant. The
# same expression runs in Python and PostgreSQL; never strip arbitrary brackets.
EDITION_LABEL = (
    r"\s*(?::\s*|\(\s*)(?:a novel|reese['’]s book club(?: pick)?|"
    r"oprah['’]s book club(?: pick)?)[\s)]*"
)
VERSION_LABEL = (
    r"\s*\(\s*(?:unabridged|abridged|older version|original recording|"
    r"revised edition|anniversary edition)\s*\)\s*"
)
NARRATOR_LABEL = r"\s*\(\s*(?:read|narrated)\s+by\s+([^()]+)\)\s*"
DISPLAY_SUFFIX = rf"(?:{EDITION_LABEL}|{VERSION_LABEL}|{NARRATOR_LABEL})+$"

# These describe different content, not merely an edition of the short title.
# Keep this guard shared between display grouping and provider matching.
DISTINCT_SUBTITLE = (
    r":\s*(?:(?:a|an|the)\s+)?(?:book|volume|vol\.?|part|summary|study guide|"
    r"workbook|companion|sequel|dramatized|adaptation|graphic novel|box set|omnibus)"
)


def distinct_work_subtitle(value):
    return bool(re.search(DISTINCT_SUBTITLE + r"\b", value, re.IGNORECASE))


def title_narrators(value):
    suffix = re.search(DISPLAY_SUFFIX, value or "", re.IGNORECASE)
    if not suffix:
        return []
    return [
        " ".join(match.group(1).split())
        for match in re.finditer(NARRATOR_LABEL, suffix.group(), re.IGNORECASE)
    ]


def display_text(value):
    value = unicodedata.normalize("NFKC", value).lower().translate(str.maketrans("‘’", "''"))
    return " ".join(value.split())


def _rule(value):
    # Fixed normalization rules must remain constants in prepared/generic plans
    # so PostgreSQL can match expression indexes. User values stay bound.
    return literal(value, literal_execute=True)


def display_text_sql(value):
    value = func.translate(
        func.lower(func.normalize(value, literal_column("NFKC"))), _rule("‘’"), _rule("''")
    )
    return func.trim(func.regexp_replace(value, _rule(r"\s+"), _rule(" "), _rule("g")))


def stripped_title(value):
    """Drop trailing edition labels while keeping the title's original casing."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    return re.sub(DISPLAY_SUFFIX, "", value, flags=re.IGNORECASE).strip()


def display_title(value):
    value = display_text(value)
    return re.sub(DISPLAY_SUFFIX, "", value).strip()


def titles_agree(expected, actual):
    """Edition labels such as (Unabridged) are not a different book."""
    left = {display_title(title) for title in expected if title and display_title(title)}
    right = {display_title(title) for title in actual if title and display_title(title)}
    return bool(left) and left <= right


def display_title_sql(value):
    value = display_text_sql(value)
    return func.trim(func.regexp_replace(value, _rule(DISPLAY_SUFFIX), _rule(""), _rule("g")))


def display_base_sql(value):
    """Candidate family for presentation grouping, including subtitle conflicts."""
    return func.trim(func.split_part(display_title_sql(value), _rule(":"), _rule(1)))


# Recording labels describe how a book was recorded, not a different book. A dramatized
# adaptation is an audio edition of the original, and "(1 of 3)" is one part of it.
_OPEN, _CLOSE = r"[\(\[]\s*", r"\s*[\)\]]"
_DRAMATIZED = r"(?:a\s+)?(?:graphic\s*audio\s+)?dramati[sz](?:ed|ation)(?:\s+adaptation)?"
_FULL_CAST = r"full[- ]cast(?:\s+(?:edition|dramati[sz]ation|production|recording))?"
_PART = r"(?:part\s+)?(\d{1,2})\s+of\s+(\d{1,2})"
_TRAILING_LABELS = (
    ("part", re.compile(rf"\s*{_OPEN}{_PART}{_CLOSE}\s*$", re.I)),
    ("part", re.compile(rf"\s+{_PART}\s*$", re.I)),
    ("dramatized", re.compile(rf"\s*{_OPEN}{_DRAMATIZED}{_CLOSE}\s*$", re.I)),
    ("dramatized", re.compile(rf"\s*:\s*{_DRAMATIZED}\s*$", re.I)),
    ("full_cast", re.compile(rf"\s*{_OPEN}{_FULL_CAST}{_CLOSE}\s*$", re.I)),
    ("unabridged", re.compile(rf"\s*{_OPEN}unabridged{_CLOSE}\s*$", re.I)),
    ("abridged", re.compile(rf"\s*{_OPEN}abridged{_CLOSE}\s*$", re.I)),
    ("version", re.compile(rf"(?:{EDITION_LABEL}|{VERSION_LABEL}|{NARRATOR_LABEL})$", re.I)),
)
# "Mistborn 2 - The Well of Ascension", "Red Rising Saga 1: Red Rising",
# "The Silo Saga Book 2 - Shift". Only a hint: a real title can look like this.
_SERIES_PREFIX = re.compile(
    r"^(?P<series>[^\d:]+?)\s+(?:book\s+|vol\.?\s+|volume\s+)?(?P<sequence>\d{1,3}(?:\.\d)?)"
    r"\s*(?:-|:)\s+(?P<title>\S.*)$",
    re.I,
)
_BOOK_SUFFIX = re.compile(r"^(?P<title>.+?),\s+book\s+(?P<sequence>\d{1,3})$", re.I)


@dataclass(frozen=True)
class TitleLabels:
    title: str
    part: int | None = None
    part_total: int | None = None
    dramatized: bool = False
    full_cast: bool = False
    abridged: bool | None = None
    series: str | None = None
    sequence: str | None = None
    series_title: str | None = None

    @property
    def recording_kind(self) -> str | None:
        if self.dramatized:
            return "dramatized"
        return "full_cast" if self.full_cast else None


def parse_title_labels(value: str | None) -> TitleLabels:
    """Split recording labels and part numbers from a library or release title.

    Summary, study guide, graphic novel, box set, and omnibus stay in the title: those
    are different content, not a recording of the book.
    """
    title = " ".join(unicodedata.normalize("NFKC", value or "").split())
    part = total = abridged = None
    dramatized = full_cast = False
    changed = True
    while changed and title:
        changed = False
        for kind, pattern in _TRAILING_LABELS:
            match = pattern.search(title)
            if not match or not title[: match.start()].strip():
                continue
            if kind == "part":
                number, count = int(match[1]), int(match[2])
                if not 1 <= number <= count <= 20 or part is not None:
                    continue
                part, total = number, count
            dramatized |= kind == "dramatized"
            full_cast |= kind == "full_cast"
            if kind in {"abridged", "unabridged"} and abridged is None:
                abridged = kind == "abridged"
            title = title[: match.start()].rstrip(" -:,")
            changed = True
            break
    series = sequence = series_title = None
    if match := _SERIES_PREFIX.match(title):
        series, sequence = match["series"].strip(), match["sequence"]
        series_title = match["title"].strip()
        series = re.sub(r"\s+(?:book|vol\.?|volume)$", "", series, flags=re.I) or None
    elif match := _BOOK_SUFFIX.match(title):
        sequence, series_title = match["sequence"], match["title"].strip()
    return TitleLabels(
        title=title,
        part=part,
        part_total=total,
        dramatized=dramatized,
        full_cast=full_cast,
        abridged=abridged,
        series=series,
        sequence=sequence,
        series_title=series_title,
    )


def base_title(value: str | None) -> str:
    return parse_title_labels(value).title


# Credits libraries list as authors although they name a cast or publisher.
_CREDITS = frozenset(
    {
        "fullcast",
        "various",
        "variousauthors",
        "variousnarrators",
        "graphicaudio",
        "graphicaudiollc",
        "bbcradio",
        "bbcradio4",
        "audiblestudios",
        "audibleoriginals",
        "unknown",
        "unknownauthor",
    }
)


def credit_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character
        for character in value
        if character.isalnum() and not unicodedata.combining(character)
    )


def identity_authors(authors: list[str]) -> tuple[list[str], list[str]]:
    """Authors that identify the book, and cast or publisher credits listed with them."""
    kept, credits = [], []
    for name in authors:
        (credits if credit_key(name) in _CREDITS else kept).append(name)
    return kept, credits


def recording_kind(title: str | None, authors: list[str]) -> str | None:
    """Dramatized or full-cast, from the title labels or a producer credit."""
    kind = parse_title_labels(title).recording_kind
    if kind:
        return kind
    credits = {credit_key(name) for name in identity_authors(authors)[1]}
    if credits & {"graphicaudio", "graphicaudiollc"}:
        return "dramatized"
    return "full_cast" if "fullcast" in credits else None
