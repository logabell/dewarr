"""Reading-order projection of provider observations, never an identity merge.

Provider compilation/featured flags are hints, not a reliable main-book set.
Keep every observation available for review and use popularity only for display.
"""

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

VERSION = 1


def number(snapshot):
    try:
        value = Decimal(str(snapshot.get("position")))
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def category(snapshot, language="en"):
    if snapshot.get("canonical_id") or snapshot.get("partial"):
        return "other"
    if language and "languages" in snapshot and language not in snapshot["languages"]:
        return "other"
    title = snapshot.get("book", {}).get("title", "")
    if re.search(r"\d\s*[-–,/]\s*\d", snapshot.get("details") or "") or re.search(
        r"\b(box(?:ed)?[ -]?set|omnibus)\b", title, re.I
    ):
        return "collection"
    position = number(snapshot)
    if position is not None and position > 0 and position % 1 == 0:
        return "main"
    return "supplement"


def project(entries, language="en"):
    """Return main display representatives plus classification of all entry IDs.

    Input pairs are (membership, work); no ORM or network dependencies. Historical
    observations without language facts remain reviewable until refreshed.
    """
    groups = defaultdict(list)
    classes = {}
    for entry, work in entries:
        kind = category(entry.snapshot, language)
        classes[entry.id] = kind
        if kind == "main":
            groups[number(entry.snapshot)].append((entry, work))
    chosen = []
    for _position, members in sorted(groups.items()):
        members.sort(
            key=lambda pair: (
                -pair[0].snapshot.get("users_count", 0),
                int(pair[0].snapshot["book"]["external_id"]),
                str(pair[0].id),
            )
        )
        chosen.append(members[0])
        for entry, _ in members[1:]:
            classes[entry.id] = "alternative"
    return chosen, classes


def full_book(snapshot):
    """Full-book eligibility shared by reviewed series requests and coverage."""
    if "languages" not in snapshot:
        return not any(snapshot.get(k) for k in ("compilation", "partial", "canonical_id"))
    return category(snapshot) in {"main", "supplement"}
