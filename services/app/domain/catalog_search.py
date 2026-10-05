"""Local title, series and identifier predicates within a visible work origin."""

import re

from sqlalchemy import Text, case, cast, exists, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB

from app.db.models import CatalogSeries, SeriesMembership, Version, WorkMetadataSource
from app.domain.catalog_titles import display_title_sql
from app.domain.title_matching import title_search_variants
from app.importing.match_evidence import identifier, identifier_matches_sql


def like_pattern(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def local_match(user, origin, query):
    query = query.strip()
    pattern = like_pattern(query)
    matches = [origin.title.ilike(pattern), cast(origin.authors, Text).ilike(pattern)]
    # "Cloud Atlas (Unabridged)" is the same title as "Cloud Atlas" for lookup.
    phrases = [like_pattern(value) for value in sorted(title_search_variants(query)) if value]
    series_patterns = sorted({pattern, *phrases})
    if phrases:
        matches.extend(display_title_sql(origin.title).ilike(value) for value in phrases)
        matches.append(
            exists(
                select(Version.id).where(
                    Version.work_id == origin.id,
                    or_(*(display_title_sql(Version.title).ilike(value) for value in phrases)),
                )
            )
        )
    # Expand only the declared series names, never arbitrary snapshot text.
    series = WorkMetadataSource.snapshot["series"]
    entries = func.jsonb_array_elements(
        case((func.jsonb_typeof(series) == "array", series), else_=cast(literal("[]"), JSONB))
    ).table_valued("value")
    name = cast(entries.c.value, JSONB)["name"].astext
    matches.append(
        exists(
            select(WorkMetadataSource.id)
            .join(entries, true())
            .where(
                WorkMetadataSource.work_id == origin.id,
                WorkMetadataSource.accepted.is_(True),
                or_(*(name.ilike(value) for value in series_patterns)),
            )
        )
    )
    matches.append(
        exists(
            select(SeriesMembership.id)
            .join(CatalogSeries)
            .where(
                SeriesMembership.work_id == origin.id,
                SeriesMembership.present.is_(True),
                CatalogSeries.owner_id == user.id,
                CatalogSeries.fetched_at.is_not(None),
                or_(*(CatalogSeries.name.ilike(value) for value in series_patterns)),
            )
        )
    )
    found = identifier("", query)
    if not found:
        found = identifier("asin", query)
    if found:
        matches.append(
            exists(
                select(Version.id).where(
                    Version.work_id == origin.id,
                    identifier_matches_sql(Version.identifiers, [found]),
                )
            )
        )
    qualified = re.fullmatch(r"(hardcover|openlibrary):\s*(\S+)", query, re.I)
    if qualified:
        provider, external_id = qualified.groups()
        matches.append(
            exists(
                select(WorkMetadataSource.id).where(
                    WorkMetadataSource.work_id == origin.id,
                    WorkMetadataSource.accepted.is_(True),
                    WorkMetadataSource.provider == provider.lower(),
                    func.lower(WorkMetadataSource.external_id) == external_id.lower(),
                )
            )
        )
    return or_(*matches)
