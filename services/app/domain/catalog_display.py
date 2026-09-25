"""Read-only grouping of library editions and recordings, scoped to the reader.

Catalog identities and correction history stay intact. Group before pagination and
reuse the same relation for covers, availability and the book's library copies.
"""

from sqlalchemy import Text, case, cast, distinct, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app.db.models import LibraryAsset, Work, WorkMetadataSource
from app.domain.catalog_language import catalog_language_sql
from app.domain.catalog_titles import (
    DISTINCT_SUBTITLE,
    display_base_sql,
    display_text_sql,
    display_title_sql,
)
from app.domain.visibility import visible_origin_work, visible_work
from app.domain.work_graph import canonical_map


def display_map(user, work_ids=None):
    from app.domain.availability import availability_rows

    canonical = canonical_map()
    # A detail/page projection needs only these title families. Include every
    # subtitle/author/language candidate so ambiguity rules remain unchanged.
    selected = None
    if work_ids is not None:
        from sqlalchemy.orm import aliased

        seed = aliased(Work)
        titles = (
            select(display_base_sql(seed.title))
            .join(canonical, canonical.c.work_id == seed.id)
            .where(canonical.c.origin_id.in_(work_ids))
        )
        selected = (
            select(Work.id)
            .where(Work.redirect_to.is_(None), display_base_sql(Work.title).in_(titles))
            .cte()
        )
    candidate = Work.id.in_(select(selected.c.id)) if selected is not None else True
    holding_candidate = (
        canonical.c.work_id.in_(select(selected.c.id)) if selected is not None else True
    )
    holdings = (
        availability_rows(user, canonical)
        .with_only_columns(
            canonical.c.work_id,
            func.bool_or(LibraryAsset.medium == "ebook").label("ebook"),
            func.bool_or(LibraryAsset.medium == "audio").label("audio"),
        )
        .where(holding_candidate)
        .group_by(canonical.c.work_id)
        .subquery()
    )

    def normalize(value):
        return display_text_sql(value)

    authors = func.jsonb_array_elements_text(Work.authors).table_valued("value")
    author = normalize(authors.c.value)
    author_key = (
        select(func.string_agg(distinct(author), aggregate_order_by("\x1f", author)))
        .select_from(authors)
        .where(author != "")
        .correlate(Work)
        .scalar_subquery()
    )
    # Title alone never establishes identity. Explicitly rejected identities
    # cannot be rejoined through a presentation preference.
    eligible = (
        author_key.is_not(None)
        & Work.metadata_fields["identity_rejected"].astext.is_distinct_from("true")
        & Work.metadata_fields["display_separate"].astext.is_distinct_from("true")
    )
    # A conflicting accepted Hardcover identity is evidence against automatic
    # title grouping. Explicitly reviewed permanent merges still take precedence.
    from sqlalchemy.orm import aliased

    source_work = aliased(Work)
    sources = (
        select(
            canonical.c.work_id,
            func.min(WorkMetadataSource.external_id).label("provider_min"),
            func.max(WorkMetadataSource.external_id).label("provider_max"),
        )
        .join(source_work, source_work.id == WorkMetadataSource.work_id)
        .join(canonical, canonical.c.origin_id == source_work.id)
        .where(
            WorkMetadataSource.provider == "hardcover",
            WorkMetadataSource.accepted.is_(True),
            visible_origin_work(user, source_work),
            holding_candidate,
        )
        .group_by(canonical.c.work_id)
        .subquery()
    )
    roots = (
        select(
            Work.id.label("root_id"),
            Work.created_at,
            sources.c.provider_min,
            sources.c.provider_max,
            (normalize(Work.title) == display_title_sql(Work.title)).label("plain_title"),
            case((eligible, display_title_sql(Work.title)), else_=cast(Work.id, Text)).label(
                "title_key"
            ),
            func.coalesce(author_key, "").label("author_key"),
            catalog_language_sql(Work.language).label("language"),
            holdings.c.ebook,
            holdings.c.audio,
        )
        .outerjoin(holdings, holdings.c.work_id == Work.id)
        .outerjoin(sources, sources.c.work_id == Work.id)
        .where(Work.redirect_to.is_(None), visible_work(user), candidate)
        .cte()
    )
    base = func.trim(func.split_part(roots.c.title_key, ":", 1))
    # Join a real short title with one unambiguous full subtitle, not arbitrary
    # books sharing a series prefix. Multiple different subtitles stay separate.
    title_groups = (
        select(
            base.label("base"),
            roots.c.author_key,
            func.count(func.distinct(roots.c.title_key)).label("titles"),
            func.bool_or(roots.c.title_key == base).label("has_short"),
            func.bool_or(roots.c.title_key.op("~")(DISTINCT_SUBTITLE + r"\y")).label(
                "different_work"
            ),
        )
        .group_by(base, roots.c.author_key)
        .subquery()
    )
    title_key = case(
        (
            title_groups.c.has_short
            & (title_groups.c.titles <= 2)
            & ~title_groups.c.different_work,
            base,
        ),
        else_=roots.c.title_key,
    )
    identity = [title_key, roots.c.author_key]
    candidates = (
        select(
            roots.c.root_id,
            roots.c.created_at,
            roots.c.plain_title,
            title_key.label("title_key"),
            roots.c.author_key,
            roots.c.language,
            func.min(roots.c.provider_min).over(partition_by=identity).label("provider_min"),
            func.max(roots.c.provider_max).over(partition_by=identity).label("provider_max"),
            func.min(roots.c.language).over(partition_by=identity).label("min_language"),
            func.max(roots.c.language).over(partition_by=identity).label("max_language"),
            roots.c.ebook,
            roots.c.audio,
        )
        .join(
            title_groups,
            (title_groups.c.base == base) & (title_groups.c.author_key == roots.c.author_key),
        )
        .subquery()
    )
    # Unknown language may join one known language, but must not bridge two
    # explicitly different translations of the same title.
    identity = [
        candidates.c.title_key,
        candidates.c.author_key,
        case(
            (
                candidates.c.provider_min != candidates.c.provider_max,
                cast(candidates.c.root_id, Text),
            ),
            else_="",
        ),
        case(
            (candidates.c.min_language != candidates.c.max_language, candidates.c.language),
            else_="",
        ),
    ]
    grouped = select(
        candidates.c.root_id,
        func.first_value(candidates.c.root_id)
        .over(
            partition_by=identity,
            order_by=[
                func.coalesce(candidates.c.ebook, False).desc(),
                candidates.c.plain_title.desc(),
                candidates.c.created_at,
                candidates.c.root_id,
            ],
        )
        .label("representative"),
        func.bool_or(candidates.c.ebook).over(partition_by=identity).label("ebook"),
        func.bool_or(candidates.c.audio).over(partition_by=identity).label("audio"),
    ).subquery()
    return (
        select(
            canonical.c.origin_id,
            case(
                (
                    func.coalesce(grouped.c.ebook, False) | func.coalesce(grouped.c.audio, False),
                    grouped.c.representative,
                ),
                else_=canonical.c.work_id,
            ).label("work_id"),
        )
        .join(grouped, grouped.c.root_id == canonical.c.work_id)
        .subquery()
    )


def display_ids(user):
    mapping = display_map(user)
    return select(mapping.c.work_id).distinct()


def display_family(user, work_id):
    mapping = display_map(user, [work_id])
    root = select(mapping.c.work_id).where(mapping.c.origin_id == work_id).scalar_subquery()
    return select(mapping.c.origin_id).where(mapping.c.work_id == root)
