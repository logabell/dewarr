"""Use selected MAM release credits only for an unambiguous single-book import."""

from app.domain.narrators import name_key


def naming_narrators(group, version, releases):
    # File credits win. Catalog recording credits already reviewed by the user
    # remain authoritative; tracker metadata only fills a missing value.
    if group.narrators:
        return group.narrators
    if version.narrators:
        return version.narrators
    if group.medium != "audio":
        return []
    candidates = {}
    for release in releases:
        if release.get("source") != "mam" or release.get("medium") != "audio":
            continue
        values = release.get("narrators") or []
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            continue
        names = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if names and len(names) <= 30 and all(len(value) <= 600 for value in names):
            candidates[tuple(sorted(name_key(value) for value in names))] = names
    # Conflicting release evidence never chooses an arbitrary narration.
    return next(iter(candidates.values())) if len(candidates) == 1 else []
