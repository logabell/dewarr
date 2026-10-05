"""Frozen, explicitly reviewed contents beside a real collection edition."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models import Work
from app.importing.naming import StrictModel, fingerprint


class ContainedWork(StrictModel):
    work_id: UUID
    title: str
    authors: list[str]
    revision: str


def identity(work):
    return fingerprint(
        {
            "id": str(work.id),
            "title": work.title,
            "authors": work.authors,
            "redirect_to": str(work.redirect_to) if work.redirect_to else None,
        }
    )


async def freeze(db, ids, physical_work_id):
    if not ids:
        return []
    unique = sorted(set(ids))
    if len(unique) != len(ids) or not 2 <= len(unique) <= 100 or physical_work_id in unique:
        raise HTTPException(
            422, "Select 2–100 distinct contained books, excluding the collection itself"
        )
    works = list(await db.scalars(select(Work).where(Work.id.in_(unique)).order_by(Work.id)))
    if len(works) != len(unique) or any(work.redirect_to for work in works):
        raise HTTPException(409, "Contained book identity changed; review its current record")
    return [
        ContainedWork(
            work_id=work.id, title=work.title, authors=work.authors, revision=identity(work)
        ).model_dump(mode="json")
        for work in works
    ]


async def verify(db, contents, *, lock=False):
    items = [ContainedWork.model_validate(saved) for saved in contents]
    if not items:
        return
    statement = (
        select(Work.id, Work.title, Work.authors, Work.redirect_to)
        .where(Work.id.in_({item.work_id for item in items}))
        .order_by(Work.id)
    )
    if lock:
        statement = statement.with_for_update(read=True)
    # Read current identity fields on every verification, independently of the ORM
    # identity map. Acquire publication locks in a consistent order when requested.
    works = {work.id: work for work in await db.execute(statement)}
    for item in items:
        work = works.get(item.work_id)
        if not work or work.redirect_to or identity(work) != item.revision:
            raise HTTPException(
                409, "Contained book identity changed; review a fresh collection plan"
            )
