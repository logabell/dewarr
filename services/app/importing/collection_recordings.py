"""Check frozen recording choices against downloaded, embedded metadata."""

import re
from pathlib import PurePosixPath

from app.domain.collection_contents import title_key


def narrator_keys(values):
    # Explicit conjunctions/semicolons separate credits; commas may be part of a name.
    return {
        title_key(part)
        for value in values
        for part in re.split(r"\s*;\s*|\s+(?:and|&)\s+", value, flags=re.I)
        if part.strip()
    }


def conflict(members, group, facts, *, allowed_members=None):
    paths = {f.path for f in group.files}
    reviewed = False
    matched = False
    for member in members:
        review = member.frozen.get("collection_review")
        if not review:
            continue
        reviewed = True
        root = member.frozen["descriptor"]["name"]
        chosen = {
            str(PurePosixPath(p).relative_to(root)) if p.startswith(root + "/") else p
            for p in review["paths"]
        }
        if not chosen.intersection(paths):
            continue
        matched = True
        if allowed_members is not None and member not in allowed_members:
            return "Collection files are mapped to a different reviewed book"
        if paths != chosen:
            return "Collection files do not form the reviewed recording; review book grouping"
        claim = review.get("recording") or {}
        narrator = claim.get("narrator_claim")
        if narrator:
            expected = narrator_keys([narrator])
            if not facts.narrators or any(
                narrator_keys(names) != expected for names in facts.narrators
            ):
                return "Embedded narrator is missing or differs from the selected recording"
        abridgment = claim.get("abridgment_claim")
        if abridgment and facts.abridged != [abridgment.casefold() == "abridged"]:
            return "Embedded abridgment is missing or differs from the selected recording"
    if reviewed and not matched:
        return "Collection files were not mapped to a reviewed book recording"
    return None
