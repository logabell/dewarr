"""Catalog-backed series labels shared by release and downloaded-file matching."""

import re
from decimal import Decimal

from app.domain.catalog_titles import display_title
from app.domain.title_matching import exact_title_key


def position_key(value):
    value = str(value if value is not None else "").strip()
    return Decimal(value) if re.fullmatch(r"\d{1,4}(?:\.\d{1,4})?", value) else None


def series_name(value):
    """Only presentation wrappers are optional, not words inside a series name."""
    value = display_title(value)
    value = re.sub(r"^(?:the|a|an)\s+", "", value)
    return re.sub(r"\s+(?:saga|series)$", "", value).strip()


def note_position(value):
    """A numeric position or canonical Roman numeral, only inside a known series note."""
    position = position_key(value)
    if position is not None:
        return position
    if not value or not re.fullmatch(
        r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})", value
    ):
        return None
    numbers = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total, previous = 0, 0
    for letter in reversed(value):
        number = numbers[letter]
        total += -number if number < previous else number
        previous = number
    return Decimal(total)


def series_note_agrees(note, series, *, require_position=False):
    # Preserve punctuation until numbers have been checked: 5.5 is not 5,
    # and 1-3 is a collection, not a single position.
    note = display_title(note)
    for entry in series:
        name = series_name(entry.get("name", ""))
        if not name:
            continue
        match = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", note)
        if not match:
            continue
        remainder = note[: match.start()] + " " + note[match.end() :]
        remainder = re.sub(
            r"\b(?:a|an|the|trilogy|duology|quartet|saga|series|cycle|book|bk|volume|vol)\b\.?",
            " ",
            remainder,
        ).strip(" ,:;#")
        # Some audiobook filenames abbreviate "track/book 3" as "T3".
        # Only strip this marker when it directly precedes a numeric position.
        remainder = re.sub(r"\bt(?=\d)", "", remainder, flags=re.I).strip(" ,:;#")
        if not remainder and not require_position:
            return True
        position = note_position(remainder)
        if position is not None and position == position_key(entry.get("position")):
            return True
    return False


def title_outside_series_note(value, work):
    """Strip only a known series annotation after the complete catalog title."""
    title = display_title(work["title"])
    value = display_title(value)
    if not title:
        return None
    series = [entry for entry in work.get("series", []) if isinstance(entry, dict)]
    for boundary in re.finditer(r"\(|:", value):
        # Compare the complete title before interpreting its annotation. Keep
        # connector/punctuation equivalence without omitting actual subtitles.
        if exact_title_key(value[: boundary.start()]) != exact_title_key(title):
            continue
        suffix = value[boundary.start() :]
        if suffix.startswith("(") and suffix.endswith(")"):
            note = suffix[1:-1]
        elif suffix.startswith(":"):
            note = suffix[1:]
        else:
            continue
        if series_note_agrees(note, series):
            return title
    return None


def title_outside_series_prefix(value, work):
    """Keep the complete title; remove only a catalog-backed series/position prefix."""
    title, value = display_title(work["title"]), display_title(value)
    if not title:
        return None
    series = [entry for entry in work.get("series", []) if isinstance(entry, dict)]
    for boundary in re.finditer(r"\s*[:–—-]\s*|\s+", value):
        if exact_title_key(value[boundary.end() :]) != exact_title_key(title):
            continue
        note = value[: boundary.start()].strip()
        if series_note_agrees(note, series, require_position=True):
            return title
    return None
