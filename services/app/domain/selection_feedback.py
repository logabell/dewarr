"""Readable summaries of bounded release selection, retaining candidate evidence."""

from collections import Counter


def rejection_reasons(payload):
    decisions = payload.get("decisions", [])
    inspected = [item for item in decisions if item.get("inspected")]
    counts = Counter(
        reason
        for item in inspected or decisions
        for reason in dict.fromkeys(item.get("reasons", []))
    )
    return [reason for reason, _ in counts.most_common(3)]


def no_release_message(payload):
    if not payload.get("decisions"):
        return "No releases were found on this search page. Try another search or source."
    reasons = rejection_reasons(payload)
    summary = "No release qualified for automatic download."
    if reasons:
        summary += " " + "; ".join(reasons)
    return summary
