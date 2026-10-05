"""Automatic candidate eligibility. Tracker claims never establish library ownership."""

import re
from pathlib import PurePosixPath

from app.domain import pack_coverage
from app.domain.acquisition import language_accepts
from app.domain.audio_manifest import distinct_numbered_tracks, single_part_name
from app.domain.release_profiles import assess_release, catalog_language_release
from app.domain.release_versions import abridgment, version_reasons
from app.domain.request_constraints import constrained_preferences
from app.domain.title_matching import compatible_title

EBOOKS = {"epub", "pdf", "cbz"}
AUDIO = {"m4b", "mp3", "flac", "aac", "ogg", "opus"}
SIDECARS = {"jpg", "jpeg", "png", "webp", "opf", "nfo", "txt", "cue", "m3u"}
PARTIAL = re.compile(r"\b(sample|excerpt|preview|incomplete|truncated)\b", re.I)
DISC_FOLDER = re.compile(r"(?:^|[\s_(\[])(?:cd|disc|disk|part)\s*\d+\b", re.I)
PACK = re.compile(
    r"\b(collection|pack|omnibus|box[ -]?set|anthology|complete series|"
    r"books?\s+\d+\s*[-–]\s*\d+)\b",
    re.I,
)


def collection_candidate(release, work, catalog=None):
    text = " ".join(
        [release.raw_title, getattr(release, "title", ""), *getattr(release, "tags", [])]
    )
    return bool(
        PACK.search(text)
        or len((release.details.get("collection_contents") or {}).get("items", [])) > 1
        or len(release.coverage) > 1
        or any(
            re.search(r"\d\s*[-–,/]\s*\d", s.position or "") for s in getattr(release, "series", [])
        )
        or (
            pack_coverage.source_series(release, work, catalog)
            # Series membership alone does not make a single book a collection.
            # Use the same title equivalence as source identity (e.g. & / and).
            and not compatible_title(getattr(release, "title", release.raw_title), work["title"])
        )
    )


def eligibility(
    release,
    work,
    rule,
    preferences,
    *,
    version=None,
    descriptor=None,
    unattended=False,
    catalog=None,
):
    release = catalog_language_release(release, work)
    preferences = constrained_preferences(preferences, rule)
    assessment = assess_release(release, work, preferences, rule["medium"])
    reasons = list(assessment.blocked)
    is_pack = collection_candidate(release, work, catalog)
    pack_sources = pack_coverage.source_series(release, work, catalog) if is_pack else []
    proof = (
        pack_coverage.manifest(release, work, catalog, descriptor, rule["medium"])
        if descriptor and pack_sources
        else None
    )
    if assessment.identity != "corroborated" and not pack_sources:
        reasons.append("The source must corroborate the catalog title and author")
    if release.medium != rule["medium"]:
        reasons.append("The source must identify the requested medium")
    required_language = rule["language"] or (version.language if version else None)
    if not language_accepts(required_language, release.language):
        reasons.append("The source does not confirm the required language")
    if release.protocol == "soulseek":
        if not getattr(release, "peer_online", False) or not getattr(release, "files", None):
            reasons.append(
                "A live Soulseek peer with a file list is required for automatic selection"
            )
        if getattr(release, "locked_files", 0):
            reasons.append("Soulseek locked some files in this folder")
        folder = str(getattr(release, "directory", "") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if DISC_FOLDER.search(folder):
            reasons.append("Soulseek returned one disc folder from a larger book")
        unknown_allowed = False
    elif release.protocol == "nzb":
        unknown_allowed = False
    else:
        unknown_allowed = (
            release.seeders is None
            and preferences.allow_unknown_seeders
            and release.source == "audiobookbay"
        )
        if release.seeders == 0 or (release.seeders is None and not unknown_allowed):
            reasons.append("At least one reported seeder is required for automatic selection")
    if unknown_allowed and descriptor and not getattr(release, "metadata_resolved", False):
        reasons.append("Unknown seed counts require resolved torrent metadata before selection")
    text = " ".join(
        [release.raw_title, getattr(release, "title", ""), *getattr(release, "tags", [])]
    )
    if PARTIAL.search(text):
        reasons.append("The source labels this release as partial content")
    if is_pack:
        if not preferences.allows_series_packs:
            reasons.append("Collection downloads are disabled by this request's preferences")
        if not pack_sources:
            reasons.append("Collection coverage needs review before automatic selection")
        elif descriptor and not proof:
            reasons.append(
                "Collection files do not uniquely establish the requested published series books"
            )
        if version or rule.get("required_narrators") or rule["abridged"] is not None:
            reasons.append(
                "Collection children need separate evidence for required edition, "
                "narrator or abridgment"
            )
    if rule["abridged"] is not None:
        # Exact source tags are claims; narration duration or prose is not an abridgment flag.
        if abridgment(release) is not rule["abridged"]:
            reasons.append("The source does not confirm the required abridgment")
    if version:
        reasons.extend(version_reasons(release, version))
    if release.protocol == "nzb":
        # Article subjects describe encoded transport files, often obfuscated
        # RAR/PAR2 volumes. Only the client's completed, unpacked output can
        # establish media contents. Keep source identity/constraints above and
        # validate actual files again in the automatic importer.
        if is_pack:
            reasons.append("Usenet collections need extracted-file coverage review")
        supported = EBOOKS if rule["medium"] == "ebook" else AUDIO
        formats = set(release.formats)
        if formats and not formats & supported:
            reasons.append("Reported formats are not supported by the automatic importer")
        preferred = (
            preferences.ebook_formats if rule["medium"] == "ebook" else preferences.audio_formats
        )
        if formats and not formats.intersection(preferred):
            reasons.append("No preferred media format is reported by the source")
        if (
            unattended
            and formats
            and not formats <= ({"epub"} if rule["medium"] == "ebook" else {"m4b", "mp3"})
        ):
            reasons.append(
                "This media format requires reviewed importing rather than automatic acquisition"
            )
        return list(dict.fromkeys(reasons))
    if descriptor:
        formats = {PurePosixPath(f.path).suffix.lower().lstrip(".") for f in descriptor.files}
        if formats & set(preferences.blocked_formats):
            reasons.append("The inspected torrent contains a blocked format")
        supported = EBOOKS if rule["medium"] == "ebook" else AUDIO
        primary = [
            f
            for f in descriptor.files
            if PurePosixPath(f.path).suffix.lower().lstrip(".") in supported
        ]
        allowed = supported | SIDECARS | ({"pdf"} if rule["medium"] == "audio" else set())
        # A same-name MOBI is an alternate copy of the one selected EPUB, not
        # another importable book. Its bytes never establish identity and are
        # left out by the importer. Keep unrelated files and multiple supported
        # editions behind review, and retain the blocked-format check above.
        companions = set()
        if rule["medium"] == "ebook" and len(primary) == 1:
            epub = PurePosixPath(primary[0].path)
            if epub.suffix.lower() == ".epub" and "epub" in preferences.ebook_formats:
                companions = {
                    f.path
                    for f in descriptor.files
                    if PurePosixPath(f.path).suffix.lower() == ".mobi"
                    and PurePosixPath(f.path).with_suffix("") == epub.with_suffix("")
                }
        if any(
            PurePosixPath(f.path).suffix.lower().lstrip(".") not in allowed
            and f.path not in companions
            for f in descriptor.files
        ):
            reasons.append("The torrent contains unsupported or ambiguous file types")
        if not primary:
            reasons.append("No supported primary media files were found")
        elif unattended and any(
            PurePosixPath(f.path).suffix.lower().lstrip(".")
            not in ({"epub"} if rule["medium"] == "ebook" else {"m4b", "mp3"})
            for f in primary
        ):
            reasons.append(
                "This media format requires reviewed importing rather than automatic acquisition"
            )
        elif not proof and rule["medium"] == "ebook" and len(primary) != 1:
            reasons.append("Multiple ebook files need edition or collection review")
        elif not proof and rule["medium"] == "audio":
            if len(primary) == 1 and single_part_name(
                PurePosixPath(primary[0].path).stem, work["title"]
            ):
                reasons.append("Audio filename identifies one part of a larger recording")
            if len(primary) > 500 or len({str(PurePosixPath(f.path).parent) for f in primary}) != 1:
                reasons.append(
                    "Audio files span multiple book folders or exceed the automatic track limit"
                )
            if len({PurePosixPath(f.path).suffix.lower() for f in primary}) != 1:
                reasons.append("Alternative audio encodings need recording review")
            if len(primary) > 1:
                if not distinct_numbered_tracks(
                    (PurePosixPath(f.path).stem for f in primary), work["title"]
                ):
                    reasons.append("Audio filenames do not establish one numbered track sequence")
        if not proof and (
            PACK.search(descriptor.name) or any(PACK.search(f.path) for f in primary)
        ):
            reasons.append("The file manifest indicates a collection requiring coverage review")
        if any(PARTIAL.search(f.path) for f in primary):
            reasons.append("The file manifest indicates partial content")
        preferred = (
            preferences.ebook_formats if rule["medium"] == "ebook" else preferences.audio_formats
        )
        requested_formats = set(pack_coverage.target_formats(proof)) if proof else formats
        if not requested_formats.intersection(preferred):
            reasons.append("No preferred media format is present in the torrent")
        if not (
            (len(descriptor.files) == 1 and "/" not in descriptor.files[0].path)
            or all(f.path.startswith(descriptor.name + "/") for f in descriptor.files)
        ):
            reasons.append("The torrent does not have one supported import root")
    elif release.formats and not set(release.formats) & (
        EBOOKS if rule["medium"] == "ebook" else AUDIO
    ):
        reasons.append("Reported formats are not supported by the automatic importer")
    return list(dict.fromkeys(reasons))
