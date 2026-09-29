"""Uploader claims and filename suggestions; never downloaded-content identity."""

import re
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from app.domain.collection_contents import title_key


def positions(value):
    """Expand only explicit, bounded position syntax, not book-count prose."""
    value = re.sub(r"^(?:books?\s*|#)", "", (value or "").strip(), flags=re.I)
    result = set()
    for part in re.split(r"\s*[,;/]\s*", value):
        match = re.fullmatch(r"#?(\d+(?:\.\d+)?)(?:\s*[-–]\s*#?(\d+))?", part)
        if not match:
            return set()
        start = Decimal(match[1])
        if match[2]:
            end = int(match[2])
            if start % 1 or not 0 <= start <= end or end - start >= 100:
                return set()
            result.update(Decimal(n) for n in range(int(start), end + 1))
        else:
            result.add(start)
        if len(result) > 100:
            return set()
    return result


def series_key(value):
    key = title_key(value)
    return re.sub(r" (?:saga|series)$", "", key.removeprefix("the "))


def range_claims(release, candidates):
    claims = []
    for series in release.series:
        wanted = positions(series.position)
        if not wanted:
            continue
        for candidate, _ in candidates:
            for member in candidate.series:
                try:
                    included = Decimal(str(member.get("position"))) in wanted
                except InvalidOperation:
                    included = False
                if series_key(member.get("name", "")) == series_key(series.name) and included:
                    claims.append(
                        (
                            candidate,
                            {
                                "basis": "MAM series range",
                                "series": series.name,
                                "position": member["position"],
                                "raw": series.position,
                            },
                        )
                    )
                    break
    return claims


def tagged_claims(release, candidates):
    """Resolve explicitly named titles in tags; never turn a count into membership."""
    for tag in release.tags:
        if re.search(r"not included|missing|wishlist|other books", tag, re.I):
            continue
        key = " " + title_key(tag) + " "
        for candidate, labels in candidates:
            if any(
                len(title_key(t).split()) >= 3 and " " + title_key(t) + " " in key for t in labels
            ):
                yield candidate, {"basis": "MAM tag", "raw": tag}


def recording_options(recordings, paths):
    options = []
    for index, claim in enumerate(recordings):
        narrator = claim.get("narrator_claim")
        notes = claim.get("recording_notes", "")
        markers = re.findall(r"\b(?:original|revised|older|new)\b", notes, re.I)
        shared_narrator = (
            narrator
            and sum(
                title_key(r.get("narrator_claim", "")) == title_key(narrator) for r in recordings
            )
            > 1
        )
        matching = []
        for path in paths:
            # Pack names are not evidence of each child recording.
            parts = PurePosixPath(path).parts
            key = " " + title_key("/".join(parts[1:] if len(parts) > 1 else parts)) + " "
            marker_match = bool(markers) and all(" " + m.lower() + " " in key for m in markers)
            if narrator:
                agrees = " " + title_key(narrator) + " " in key
                if shared_narrator:
                    agrees = agrees and marker_match
            else:
                agrees = marker_match
            if agrees:
                matching.append(path)
        options.append({"id": str(index), "claims": claim, "files": matching})
    # A path that still matches several recording claims cannot safely be a default.
    for option in options:
        others = {p for other in options if other is not option for p in other["files"]}
        option["ambiguous_files"] = sorted(set(option["files"]) & others)
    for option in options:
        ambiguous = set(option.pop("ambiguous_files"))
        option["files"] = [p for p in option["files"] if p not in ambiguous]
    # A sole recording claim can propose the title's files, but is still checked
    # against embedded narrator/abridgment metadata during import.
    if len(options) == 1 and not options[0]["files"]:
        options[0]["files"] = paths
    return options
