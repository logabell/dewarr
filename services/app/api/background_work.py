"""Settings -> System -> Background work: how often scheduled jobs do their work."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert

from app.api.dependencies import Admin, Database
from app.db.models import AuditEvent, BackgroundWorkSettings
from app.jobs import pace

router = APIRouter(prefix="/background-work", tags=["background work"])

Pace = Literal["normal", "relaxed", "light", "minimal"]


class BackgroundWork(BaseModel):
    pace: Pace = "normal"


@router.get("", response_model=BackgroundWork)
async def background_work(admin: Admin, db: Database):
    row = await db.get(BackgroundWorkSettings, 1)
    return BackgroundWork(pace=row.pace if row else "normal")


@router.put("", response_model=BackgroundWork)
async def save_background_work(body: BackgroundWork, admin: Admin, db: Database):
    await db.execute(
        insert(BackgroundWorkSettings)
        .values(id=1, pace=body.pace)
        .on_conflict_do_update(index_elements=[BackgroundWorkSettings.id], set_={"pace": body.pace})
    )
    db.add(
        AuditEvent(actor_id=admin.id, action="background_work.changed", detail=body.model_dump())
    )
    await db.commit()
    pace.remember(body.pace)
    return body
