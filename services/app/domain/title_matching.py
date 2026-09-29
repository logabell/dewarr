"""Title compatibility when authors and/or edition identifiers corroborate identity.

This is not a work key or a similarity search. Different primary titles and
content-bearing subtitles remain distinct, even when an identifier is shared.
"""

import re
import unicodedata
from difflib import SequenceMatcher

from app.domain.catalog_titles import display_title, distinct_work_subtitle, stripped_title

TITLE_MATCH_VERSION = 3


def title_search_variants(value):
    """Bounded connector spellings for candidate discovery, never work identity.

    Keep catalog display keys/indexes unchanged. Candidates still need author,
    edition and ambiguity checks; embedded initials such as AT&T stay literal.
    """
    value = display_title(value)
    return {value, value.replace(" & ", " and "), value.replace(" and ", " & ")}


def words(value):
    text = unicodedata.normalize("NFKD", value.casefold())
    text = re.sub(r"(?<=\s)&(?=\s)", "and", text)
    return re.findall(r"[^\W_]+", "".join(c for c in text if not unicodedata.combining(c)))


def primary(value):
    tokens = words(value)
    if len(tokens) > 2 and tokens[0] in {"a", "an", "the"}:
        tokens = tokens[1:]
    return tokens


def compatible_title(left, right, *, identified=False, allow_extra_subtitle=False):
    """Accept omitted descriptive subtitles; minor variants need a verified ID.

    Callers must separately check authors, language, edition and uniqueness.
    No fuzzy primary-title matching or conflicting volume numbers is allowed.
    """
    left, right = (stripped_title(value or "") for value in (left, right))
    if not left or not right:
        return False
    if words(left) == words(right):
        return True
    if distinct_work_subtitle(left) or distinct_work_subtitle(right):
        return False
    a, _, sub_a = left.partition(":")
    b, _, sub_b = right.partition(":")
    # A missing colon between title and subtitle is punctuation only.
    if primary(left) == primary(right):
        return True
    if primary(a) != primary(b):
        return False
    if not sub_a or not sub_b:
        return not sub_a or identified or allow_extra_subtitle
    x, y = words(sub_a), words(sub_b)
    if x == y:
        return True
    if not identified or min(len(x), len(y)) < 8:
        return False
    # Numbers and negative/edition qualifiers can change meaning despite a
    # near-identical long subtitle. They must never disappear in fuzzy matching.
    significant = {"no", "not", "without", "with", "abridged", "unabridged", "revised"}

    def sensitive(tokens):
        return {v for v in tokens if any(c.isdigit() for c in v) or v in significant}

    if sensitive(x) != sensitive(y):
        return False
    return (
        len(set(x) & set(y)) / max(len(set(x)), len(set(y))) >= 0.8
        and SequenceMatcher(None, " ".join(x), " ".join(y), autojunk=False).ratio() >= 0.92
    )
