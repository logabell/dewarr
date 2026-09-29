"""Bounded uploader contents claims, with normalized-line provenance.

Claims propose review candidates; neither bibliography nor description grants
import authority. The inspected torrent establishes downloadable files.
"""

import re

from app.adapters.mam import plain

MAX_TEXT = 100000
MAX_ROWS = 100
PARSER_VERSION = 1


def title_key(value):
    return " ".join(re.findall(r"\w+", value.casefold().replace("&", " and ")))


def extract(description):
    limited = len(description) > MAX_TEXT
    text = description[:MAX_TEXT]
    text = re.sub(r"</(?:blockquote|ol|ul)\s*>", "<br>__CONTENTS_END__<br>", text, flags=re.I)
    text = re.sub(r"\[\*\]", "\n", text)
    text = re.sub(r"\[/?(?:b|i|u|size|color|list)(?:=[^\]]*)?\]", "", text, flags=re.I)
    lines = (plain(text, MAX_TEXT) or "").splitlines()
    active, kind, negative = False, "unspecified", False
    items, excluded = {}, []
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        if line == "__CONTENTS_END__":
            active, negative = False, False
            continue
        if re.match(r"^(?:publisher|runtime|bitrate|file spec|unabridged):", line, re.I):
            continue
        if "http://" in line or "https://" in line or len(line) > 250:
            active = False
            continue
        if re.search(r"^(?:not included|missing|wishlist|other books by)\b", line, re.I):
            active, negative = False, True
            continue
        if re.search(
            r"books are as follows|^(?:includes|included books|books included|contents|"
            r"alternate versions|narrators and abridged/unabridged)\s*[:;]$",
            line,
            re.I,
        ):
            active, negative, kind = True, False, "unspecified"
            continue
        if re.search(r"^tales from .+ include:", line, re.I):
            active, negative, kind = True, False, "short_story"
            continue
        if line.endswith("Series:"):
            continue
        if re.match(r"^(?:thank you|hope that|last edited|added:|#\d|enjoy[!.]?$)", line, re.I):
            active = False
            continue
        dated_end = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\.\s*)?(.+?)\s*\((\d{4})\)", line)
        dated_start = re.fullmatch(r"((?:19|20)\d{2})\s*[-–]\s*(.+)", line)
        credit = re.fullmatch(r"(.+?) - (.+?) - ((?:un)?abridged)(?:\s*\(.*\))?", line)
        facts = {"evidence_line": line_number, "raw": raw, "content_kind_hint": kind}
        if dated_end:
            title = dated_end[2]
            facts.update(year_claim=int(dated_end[3]), source_order=dated_end[1])
        elif dated_start:
            title = dated_start[2]
            facts["year_claim"] = int(dated_start[1])
            annotation = re.search(r"\s*\(([^()]*)\)$", title)
            if annotation and re.search(
                r"read by|DT\d|version|edition|nonfiction|novella|short stor|with ",
                annotation[1],
                re.I,
            ):
                title = title[: annotation.start()].strip()
                facts["recording_notes"] = annotation[1]
                narrator = re.search(r"read by (.+)", annotation[1], re.I)
                if narrator:
                    facts["narrator_claim"] = narrator[1]
        elif active and credit:
            title = credit[1]
            facts.update(narrator_claim=credit[2], abridgment_claim=credit[3])
        elif active or negative:
            title = re.sub(r"^\d+(?:\.\d+)?\.\s+", "", line)
        else:
            continue
        if negative:
            excluded.append({"title": title, **facts})
            continue
        if len(items) >= MAX_ROWS:
            limited = True
            break
        entry = items.setdefault(
            title_key(title), {"title": title, "evidence": [], "recordings": []}
        )
        entry["evidence"].append(facts)
        if any(
            k in facts
            for k in ("narrator_claim", "recording_notes", "abridgment_claim", "year_claim")
        ):
            recording = {
                k: v
                for k, v in facts.items()
                if k not in {"evidence_line", "raw", "source_order", "content_kind_hint"}
            }
            if recording not in entry["recordings"]:
                entry["recordings"].append(recording)
    return {
        "items": list(items.values()),
        "excluded": excluded,
        "truncated": limited,
        "parser_version": PARSER_VERSION,
        "evidence_basis": "normalized description line",
    }
