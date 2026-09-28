"""Catalog and torrent-manifest corroboration, never proof of owned book contents."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from sqlalchemy import select

from app.db.models import CatalogSeries, SeriesMembership, Work
from app.domain.release_profiles import normalized
from app.domain.visibility import visible_work
from app.domain.work_graph import canonical_map, family_ids

CATALOG_FRESH_FOR = timedelta(hours=24)
MAX_ADDITIONAL_BOOKS = 20
MAX_CATALOG_MEMBERS = 200
PRIMARY = {
    "ebook": {"epub", "pdf", "cbz"},
    "audio": {"m4b", "mp3", "flac", "aac", "ogg", "opus"},
}


async def catalog(db, user, work):
    """Freeze a bounded accessible series context; missing/ambiguous records stay excluded."""
    rows = list(
        await db.scalars(
            select(CatalogSeries)
            .where(
                CatalogSeries.owner_id == user.id,
                CatalogSeries.fetched_at >= datetime.now(UTC) - CATALOG_FRESH_FOR,
                CatalogSeries.id.in_(
                    select(SeriesMembership.series_id).where(
                        SeriesMembership.present.is_(True),
                        SeriesMembership.work_id.in_(family_ids(work.id)),
                    )
                ),
            )
            .order_by(CatalogSeries.provider, CatalogSeries.external_id)
            .limit(3)
        )
    )
    mapping = canonical_map()
    series = []
    today = datetime.now(UTC).date().isoformat()
    for row in rows:
        entries = (
            await db.execute(
                select(SeriesMembership, Work)
                .join(mapping, mapping.c.origin_id == SeriesMembership.work_id)
                .join(Work, Work.id == mapping.c.work_id)
                .where(
                    SeriesMembership.series_id == row.id,
                    SeriesMembership.present.is_(True),
                    visible_work(user),
                )
                .order_by(Work.id, SeriesMembership.external_id)
                .limit(MAX_CATALOG_MEMBERS + 1)
            )
        ).all()
        if len(entries) > MAX_CATALOG_MEMBERS:
            continue
        positions = {}
        for entry, member in entries:
            positions.setdefault(entry.snapshot.get("position"), set()).add(member.id)
        members = {}
        for entry, member in entries:
            facts = entry.snapshot
            if (
                any(facts.get(k) for k in ("compilation", "partial", "canonical_id"))
                or not facts.get("release_date")
                or facts["release_date"] > today
                or facts.get("position") is None
                or len(positions[facts["position"]]) != 1
            ):
                continue
            members[str(member.id)] = {
                "id": str(member.id),
                "title": member.title,
                "authors": member.authors,
                "position": facts["position"],
                "release_date": facts["release_date"],
            }
        if str(work.id) in members and len(members) > 1:
            series.append(
                {
                    "id": str(row.id),
                    "generation": row.generation,
                    "provider": row.provider,
                    "external_id": row.external_id,
                    "name": row.name,
                    "members": list(members.values()),
                }
            )
    return {"target_id": str(work.id), "series": series}


def source_series(release, work, context):
    if not context or not {normalized(a) for a in release.authors}.intersection(
        normalized(a) for a in work["authors"]
    ):
        return []
    names = {normalized(s.name) for s in getattr(release, "series", [])}
    for value in (getattr(release, "title", ""), release.raw_title):
        names.add(normalized(value))
        names.add(
            normalized(
                re.sub(
                    r"\s*(?:[-:–]\s*)?(?:complete series|box[ -]?set|"
                    r"books?\s+\d+\s*[-–]\s*\d+)\s*$",
                    "",
                    value,
                    flags=re.I,
                )
            )
        )
    return [s for s in context["series"] if normalized(s["name"]) in names]


def label(value):
    # Only an explicit leading position is removed. Substrings and series-number
    # inference cannot identify a book or turn an omnibus into separate children.
    return normalized(re.sub(r"^(?:book\s*)?\d+(?:\.\d+)?\s*[-–.:]\s*", "", value, flags=re.I))


def manifest(release, work, context, descriptor, medium):
    candidates = source_series(release, work, context)
    primary = [
        f for f in descriptor.files if PurePosixPath(f.path).suffix[1:].lower() in PRIMARY[medium]
    ]
    proofs = []
    for series in candidates:
        groups = {}
        valid = True
        for file in primary:
            path = PurePosixPath(file.path)
            # Exclude the torrent root: a release named like one book must not
            # assign every unrelated child file to that book.
            labels = {label(path.stem), *(label(p) for p in path.parts[1:-1])}
            matches = [m for m in series["members"] if normalized(m["title"]) in labels]
            if len(matches) != 1:
                valid = False
                break
            member = matches[0]
            group = groups.setdefault(member["id"], {"work": member, "files": []})
            group["files"].append(file)
        if not valid or not 2 <= len(groups) <= MAX_ADDITIONAL_BOOKS + 1:
            continue
        if context["target_id"] not in groups:
            continue
        for group in groups.values():
            files = group["files"]
            if medium == "ebook":
                valid = len(files) == 1
            else:
                paths = [PurePosixPath(f.path) for f in files]
                valid = (
                    len(files) <= 500
                    and len({p.parent for p in paths}) == 1
                    and len({p.suffix.lower() for p in paths}) == 1
                )
                if valid and len(files) > 1:
                    title = normalized(group["work"]["title"])
                    stems = [normalized(p.stem).removeprefix(title).strip() for p in paths]
                    valid = all(
                        re.fullmatch(
                            r"(?:(?:disc|cd|part)\s*\d+\s*)?(?:(?:track|chapter)\s*)?\d+", stem
                        )
                        for stem in stems
                    ) and len(set(stems)) == len(stems)
            if not valid:
                break
        if valid:
            proofs.append(
                {
                    "series_id": series["id"],
                    "series_generation": series["generation"],
                    "series_name": series["name"],
                    "target_id": context["target_id"],
                    "evidence": "catalog-and-manifest",
                    "members": [
                        {"work": g["work"], "files": [f.path for f in g["files"]]}
                        for _, g in sorted(groups.items())
                    ],
                }
            )
    # A release associated with multiple catalog series remains reviewable.
    return proofs[0] if len(proofs) == 1 else None


def target_formats(proof):
    return sorted(
        {
            PurePosixPath(path).suffix.lower().lstrip(".")
            for member in proof["members"]
            if member["work"]["id"] == proof["target_id"]
            for path in member["files"]
        }
    )
