"""Combine the separate part items of a recording into one Audiobookshelf book, or undo it."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.dependencies import Admin, Database
from app.api.operations import OperationView
from app.config import get_settings
from app.db.models import Integration, Library, Version
from app.domain.work_graph import family_ids
from app.importing.combine import request_combine, statuses

router = APIRouter(prefix="/library", tags=["library"])


class CombineStatus(BaseModel):
    library_id: UUID
    library_name: str = ""
    version_id: UUID
    total: int
    present: list[int]
    state: Literal[
        "waiting",
        "ready",
        "skipped",
        "combining",
        "combined",
        "separating",
        "separated",
        "needs-attention",
    ]
    reason: str | None = None
    folder: str | None = None
    can_combine: bool = False
    can_separate: bool = False


class CombineRequest(BaseModel):
    library_id: UUID


async def named(db, found):
    names = dict(
        (
            await db.execute(
                select(Library.id, Library.name).where(
                    Library.id.in_({value["library_id"] for value in found})
                )
            )
        ).all()
        if found
        else []
    )
    return [
        CombineStatus(**value, library_name=names.get(value["library_id"], ""))
        for value in sorted(found, key=lambda value: (str(value["library_id"]), value["total"]))
    ]


@router.get("/works/{work_id}/part-sets", response_model=list[CombineStatus])
async def work_part_sets(work_id: UUID, admin: Admin, db: Database):
    versions = list(
        await db.scalars(select(Version.id).where(Version.work_id.in_(family_ids(work_id))))
    )
    return await named(db, list((await statuses(db, versions)).values()) if versions else [])


async def queue(db, admin, version_id, body, action):
    if get_settings().recovery_mode:
        raise HTTPException(409, "Library changes are paused during recovery")
    library = await db.get(Library, body.library_id)
    integration = await db.get(Integration, library.integration_id) if library else None
    if not integration or integration.kind != "audiobookshelf" or not integration.enabled:
        raise HTTPException(404, "Audiobookshelf library not found")
    current = (await statuses(db, [version_id])).get((body.library_id, version_id))
    if not current:
        raise HTTPException(404, "This book has no parts in that library")
    allowed = current["can_combine"] if action == "combine" else current["can_separate"]
    if not allowed:
        raise HTTPException(
            409,
            current["reason"]
            or (
                "These parts can't be combined right now"
                if action == "combine"
                else "Only a combined book can be separated"
            ),
        )
    operation = await request_combine(db, admin.id, body.library_id, version_id, action)
    await db.commit()
    await db.refresh(operation)
    return operation


@router.post("/versions/{version_id}/combine", response_model=OperationView, status_code=202)
async def combine_parts(version_id: UUID, body: CombineRequest, admin: Admin, db: Database):
    return await queue(db, admin, version_id, body, "combine")


@router.post("/versions/{version_id}/separate", response_model=OperationView, status_code=202)
async def separate_parts(version_id: UUID, body: CombineRequest, admin: Admin, db: Database):
    return await queue(db, admin, version_id, body, "separate")
