"""Bounded catalog-derived search terms, never authorization for pack coverage."""

import unicodedata
from types import SimpleNamespace

from sqlalchemy import select

from app.db.models import CatalogSeries, SeriesMembership, Version, Work, WorkMetadataSource
from app.domain.catalog_titles import identity_authors, optional_subtitle_base, parse_title_labels
from app.domain.visibility import visible_origin_work
from app.domain.work_graph import family_ids

MAX_SERIES_QUERIES = 3
MAX_CATALOG_SOURCES = 50


def normalized_query(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def default_query(work):
    """Base title plus the first author's surname.

    Recording labels like "(1 of 3)" are not in release names, and a surname
    survives "J.K." versus "J. K." spellings that a full name would not.
    """
    title = parse_title_labels(work.title).title or work.title
    authors = identity_authors(work.authors)[0]
    surname = authors[0].split()[-1].strip(",.") if authors and authors[0].split() else ""
    query = (
        f"{title} {surname}" if surname and surname.casefold() not in title.casefold() else title
    )
    return query[:300]


def book_queries(work, query):
    """Broaden a default search after empty responses; preserve custom queries.

    Search terms discover candidates, never establish their identity. Keep
    content-bearing subtitles (volumes, summaries, etc.) even in the broad query.
    """
    work = SimpleNamespace(**work)
    if normalized_query(query) != normalized_query(default_query(work)):
        return [query]
    title = optional_subtitle_base(work.title)
    shortened = default_query(SimpleNamespace(title=title, authors=work.authors))
    candidates, seen = [], set()
    for value in (query, shortened, title[:300]):
        key = normalized_query(value)
        if key and key not in seen:
            candidates.append(value)
            seen.add(key)
    return candidates


async def edition_identifiers(db, work, medium):
    """ISBN and ASIN values of the book's editions, to corroborate source listings."""
    rows = await db.scalars(
        select(Version.identifiers).where(
            Version.work_id.in_(family_ids(work.id)),
            *(() if medium == "all" else (Version.medium == medium,)),
        )
    )
    values = {
        value.strip()
        for identifiers in rows
        for key in ("isbn", "isbn10", "isbn13", "isbn_10", "isbn_13", "asin")
        if isinstance(value := (identifiers or {}).get(key), str) and value.strip()
    }
    return sorted(values)[:50]


def same_scope(left, right):
    def scope(plan):
        return [
            {
                **term,
                "evidence": [
                    {key: value for key, value in evidence.items() if key != "observed_at"}
                    for evidence in term["evidence"]
                ],
            }
            for term in plan["queries"]
        ]

    return scope(left) == scope(right)


async def plan(db, user, work, query, enabled):
    queries = [{"key": "book", "kind": "book", "query": query, "evidence": []}]
    if not enabled:
        return {"queries": queries, "warnings": []}
    sources = list(
        await db.scalars(
            select(WorkMetadataSource)
            .join(Work, Work.id == WorkMetadataSource.work_id)
            .where(
                WorkMetadataSource.work_id.in_(family_ids(work.id)),
                WorkMetadataSource.accepted.is_(True),
                visible_origin_work(user),
            )
            .order_by(
                WorkMetadataSource.provider, WorkMetadataSource.external_id, WorkMetadataSource.id
            )
            .limit(MAX_CATALOG_SOURCES + 1)
        )
    )
    catalogs = (
        await db.execute(
            select(CatalogSeries, SeriesMembership)
            .join(SeriesMembership)
            .where(
                CatalogSeries.owner_id == user.id,
                CatalogSeries.fetched_at.is_not(None),
                SeriesMembership.present.is_(True),
                SeriesMembership.work_id.in_(family_ids(work.id)),
            )
            .order_by(
                CatalogSeries.provider, CatalogSeries.external_id, SeriesMembership.external_id
            )
            .limit(MAX_CATALOG_SOURCES + 1)
        )
    ).all()
    warnings, candidates = [], []
    if len(sources) > MAX_CATALOG_SOURCES or len(catalogs) > MAX_CATALOG_SOURCES:
        warnings.append("Series search evidence is limited to 50 catalog records per source type")
    for catalog, member in catalogs[:MAX_CATALOG_SOURCES]:
        candidates.append(
            (
                catalog.name,
                {
                    "kind": "series-catalog",
                    "provider": catalog.provider,
                    "external_id": catalog.external_id,
                    "record_id": str(catalog.id),
                    "member_id": member.external_id,
                    "observed_at": catalog.fetched_at.isoformat(),
                },
            )
        )
    for source in sources[:MAX_CATALOG_SOURCES]:
        entries = source.snapshot.get("series", [])
        if len(entries) > 50:
            warnings.append("Series search evidence is limited to 50 names per metadata record")
        for entry in entries[:50]:
            if entry.get("compilation"):
                continue
            candidates.append(
                (
                    entry.get("name", ""),
                    {
                        "kind": "book-metadata",
                        "provider": source.provider,
                        "external_id": str(entry.get("external_id", "")),
                        "record_id": str(source.id),
                        "observed_at": source.fetched_at.isoformat(),
                    },
                )
            )
    names = {}
    # The title-and-author query already finds a series named like the book.
    primary = {normalized_query(query), normalized_query(parse_title_labels(work.title).title)}
    for name, evidence in candidates:
        if not isinstance(name, str):
            continue
        name = " ".join(name.split())
        if not name or len(name) > 300 or any(unicodedata.category(c) == "Cc" for c in name):
            warnings.append("An empty, overlong or unsupported series name was not searched")
            continue
        normalized = normalized_query(name)
        if normalized in primary:
            continue
        item = names.setdefault(normalized, {"query": name, "evidence": []})
        if evidence not in item["evidence"]:
            item["evidence"].append(evidence)
    if len(names) > MAX_SERIES_QUERIES:
        warnings.append("Searching at most 3 known series names; use a custom query for others")
    for index, (_, item) in enumerate(sorted(names.items())[:MAX_SERIES_QUERIES]):
        queries.append({"key": f"series:{index}", "kind": "series", **item})
    return {"queries": queries, "warnings": list(dict.fromkeys(warnings))}
