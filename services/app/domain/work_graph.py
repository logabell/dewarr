"""Canonical work identities with immutable, provenance-bearing origin bindings."""

import hashlib
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from app.db.models import Work
from app.domain.operations import transaction_lock


def canonical_map():
    paths = select(Work.id.label("origin_id"), Work.id.label("work_id"), Work.redirect_to).cte(
        recursive=True
    )
    paths = paths.union_all(
        select(paths.c.origin_id, Work.id, Work.redirect_to).join(
            paths, Work.id == paths.c.redirect_to
        )
    )
    return (
        select(paths.c.origin_id, paths.c.work_id)
        .where(paths.c.redirect_to.is_(None))
        .distinct()
        .subquery()
    )


def family_ids(work_id):
    """All origins whose canonical root is the root of the given work."""
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
