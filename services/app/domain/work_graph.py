"""Canonical work identities with immutable, provenance-bearing origin bindings."""

import hashlib
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from app.db.models import Work
from app.domain.operations import transaction_lock


def canonical_map(origin_ids=None):
    seed = select(Work.id.label("origin_id"), Work.id.label("work_id"), Work.redirect_to)
    if origin_ids is not None:
        seed = seed.where(Work.id.in_(origin_ids))
    paths = seed.cte(recursive=True)
    step = select(paths.c.origin_id, Work.id, Work.redirect_to).join(
        paths, Work.id == paths.c.redirect_to
    )
    # Bound point/page traversals even if a legacy chain contains a cycle.
    paths = paths.union(step) if origin_ids is not None else paths.union_all(step)
    return (
        select(paths.c.origin_id, paths.c.work_id)
        .where(paths.c.redirect_to.is_(None))
        .distinct()
        .subquery()
    )


def canonical_families(root_ids):
    """Map all origins belonging to selected canonical roots, using the redirect index."""
    paths = (
        select(Work.id.label("origin_id"), Work.id.label("work_id"))
        .where(Work.id.in_(root_ids), Work.redirect_to.is_(None))
        .cte(recursive=True)
    )
    paths = paths.union(
        select(Work.id, paths.c.work_id).join(paths, Work.redirect_to == paths.c.origin_id)
    )
    return select(paths).subquery()


def family_ids(work_id):
    """All origins whose canonical root is the root of the given work."""
    if isinstance(work_id, UUID | str):
        # Point lookups must not build two maps of the entire catalog. Walk
        # upwards to the root, then use the redirect index to find its children.
        # UNION also terminates malformed cycles without inventing a root.
        ancestors = select(Work.id, Work.redirect_to).where(Work.id == work_id).cte(recursive=True)
        ancestors = ancestors.union(
            select(Work.id, Work.redirect_to).join(ancestors, Work.id == ancestors.c.redirect_to)
        )
        family = select(ancestors.c.id).where(ancestors.c.redirect_to.is_(None)).cte(recursive=True)
        family = family.union(select(Work.id).join(family, Work.redirect_to == family.c.id))
        return select(family.c.id)
    # Set-based callers pass an outer SQL column, not one book id. Keep their
    # maps independent so each row retains its own correlated family predicate.
    mapping = canonical_map()
    roots = canonical_map()
    # A second map keeps this scalar from correlating against the outer family
    # rows. Reusing one subquery makes Postgres see every intent as a match.
    root = select(roots.c.work_id).where(roots.c.origin_id == work_id).limit(1).scalar_subquery()
    return select(mapping.c.origin_id).where(mapping.c.work_id == root)


async def canonical_work(db, work_id):
    seen = set()
    while work_id not in seen:
        seen.add(work_id)
        work = await db.get(Work, work_id, populate_existing=True)
        if not work:
            raise HTTPException(404, "Book not found")
        if not work.redirect_to:
            return work
        work_id = work.redirect_to
    raise HTTPException(409, "This book's identity relationship needs repair")


async def graph_lock(db, *, exclusive=False):
    number = int.from_bytes(hashlib.sha256(b"identity:work-graph").digest()[:8], signed=True)
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await db.execute(text(f"SELECT {function}(:key)"), {"key": number})


async def acquisition_lock(db, work_id: UUID):
    await graph_lock(db)
    # Acquire admission before any work lock: batch requests can span many works.
    # One transaction gate avoids quota/work lock inversions during parallel batches.
    await transaction_lock(db, "request-quotas:admission")
    work = await canonical_work(db, work_id)
    await transaction_lock(db, "acquisition:" + str(work.id))
    return work
