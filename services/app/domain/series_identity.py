"""Catalog-backed series labels shared by release and downloaded-file matching."""

import re
from decimal import Decimal

from app.domain.catalog_titles import display_title


def position_key(value):
    value = str(value if value is not None else "").strip()
    return Decimal(value) if re.fullmatch(r"\d{1,4}(?:\.\d{1,4})?", value) else None


def series_note_agrees(note, series):
    # Preserve punctuation until numbers have been checked: 5.5 is not 5,
    # and 1-3 is a collection, not a single position.
    note = display_title(note)
    for entry in series:
        name = display_title(entry.get("name", ""))
        if not name:
            continue
        match = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", note)
        if not match:
            continue
        remainder = note[: match.start()] + " " + note[match.end() :]
        numbers = re.findall(r"\d+(?:\.\d+)?", remainder)
        if numbers and (
            len(numbers) != 1
            or position_key(numbers[0]) is None
            or position_key(numbers[0]) != position_key(entry.get("position"))
        ):
            continue
        remainder = re.sub(r"\d+(?:\.\d+)?", " ", remainder)
        remainder = re.sub(
            r"\b(?:a|an|the|trilogy|duology|quartet|saga|series|cycle|book|volume|vol)\b\.?",
            " ",
            remainder,
        )
        if not remainder.strip(" ,:;#"):
            return True
    return False


def title_outside_series_note(value, work):
    """Strip only a known series annotation after the complete catalog title."""
    title = display_title(work["title"])
    value = display_title(value)
    if not title or not value.startswith(title):
        return None
    suffix = value[len(title) :].strip()
    if suffix.startswith("(") and suffix.endswith(")"):
        note = suffix[1:-1]
    elif suffix.startswith(":"):
        note = suffix[1:]
    else:
        return None
    series = [entry for entry in work.get("series", []) if isinstance(entry, dict)]
    return title if series_note_agrees(note, series) else None
