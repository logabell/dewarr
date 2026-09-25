"""Resolve accepted provider identities without inferring a match from title text."""

from sqlalchemy import select, tuple_

from app.db.models import Work, WorkMetadataSource
from app.domain.catalog_display import display_map
from app.domain.visibility import visible_origin_work, visible_work


async def visible_provider_works(db, user, identities):
    identities = set(identities)
    if not identities:
        return {}
    mapping = display_map(
        user,
        select(WorkMetadataSource.work_id).where(
            tuple_(WorkMetadataSource.provider, WorkMetadataSource.external_id).in_(identities),
            WorkMetadataSource.accepted.is_(True),
        ),
    )
    candidates = {}
    for provider, external_id, root in await db.execute(
        select(WorkMetadataSource.provider, WorkMetadataSource.external_id, mapping.c.work_id)
        .join(Work, Work.id == WorkMetadataSource.work_id)
        .join(mapping, mapping.c.origin_id == Work.id)
        .where(
            tuple_(WorkMetadataSource.provider, WorkMetadataSource.external_id).in_(identities),
            WorkMetadataSource.accepted.is_(True),
            visible_origin_work(user),
        )
    ):
        candidates.setdefault((provider, external_id), set()).add(root)
    roots = {next(iter(values)) for values in candidates.values() if len(values) == 1}
    works = {
        work.id: work
        for work in await db.scalars(select(Work).where(Work.id.in_(roots), visible_work(user)))
    }
    return {
        key: works[next(iter(values))]
        for key, values in candidates.items()
        if len(values) == 1 and next(iter(values)) in works
    }


async def displayed_provider_works(db, user, provider, books):
    """Project ownership for unique exact title/author matches without saving a binding.

    Accepted provider links take precedence. Unlinked provider books may use an
    unambiguous owned local work with compatible language. Rejected or ambiguous
    existing provider links are never overridden by this display-only fallback.
    """
    from app.domain.catalog_language import catalog_language
    from app.domain.catalog_titles import display_text, display_title, display_title_sql

    def display_key(title, authors):
        # Do not let empty credits establish identity; duplicate source credits
        # should agree with the set of names used by SQL display grouping.
        names = sorted({display_text(name) for name in authors if display_text(name)})
        return (display_title(title), tuple(names)) if names else None

    books = list(books)
    identities = {(provider, book.external_id) for book in books}
    matched = await visible_provider_works(db, user, identities)
    missing = identities - matched.keys()
    if not missing:
        return matched
    blocked = set(
        await db.execute(
            select(WorkMetadataSource.provider, WorkMetadataSource.external_id)
            .join(Work, Work.id == WorkMetadataSource.work_id)
            .where(
                tuple_(WorkMetadataSource.provider, WorkMetadataSource.external_id).in_(missing),
                visible_origin_work(user),
            )
        )
    )
    pending = [
        book
        for book in books
        if (provider, book.external_id) in missing - blocked
        and display_key(book.title, book.authors)
    ]
    if not pending:
        return matched
    title = display_title_sql(Work.title)
    mapping = display_map(
        user, select(Work.id).where(title.in_({display_title(book.title) for book in pending}))
    )
    rows = list(
        await db.execute(
            select(Work, mapping.c.work_id)
            .join(mapping, mapping.c.origin_id == Work.id)
            .where(
                title.in_({display_title(book.title) for book in pending}),
                visible_origin_work(user),
                Work.metadata_fields["identity_rejected"].astext.is_distinct_from("true"),
            )
        )
    )
    by_identity = {}
    for work, root in rows:
        key = display_key(work.title, work.authors)
        if key:
            by_identity.setdefault(key, []).append((work, root))
    resolved = {}
    for book in pending:
        language = catalog_language(getattr(book, "language", None))
        roots = {
            root
            for work, root in by_identity.get(display_key(book.title, book.authors), [])
            if not language
            or not catalog_language(work.language)
            or catalog_language(work.language) == language
        }
        if len(roots) == 1:
            resolved[(provider, book.external_id)] = next(iter(roots))
    if resolved:
        from app.domain.availability import availability_for

        available = await availability_for(db, user, list(set(resolved.values())))
        works = {
            work.id: work
            for work in await db.scalars(
                select(Work).where(Work.id.in_(set(resolved.values())), visible_work(user))
            )
        }
        matched.update(
            {
                key: works[root]
                for key, root in resolved.items()
                if root in works and available[root].owned
            }
        )
    return matched
