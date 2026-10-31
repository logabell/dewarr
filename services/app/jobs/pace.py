"""
How often scheduled background jobs do their work, from Settings -> System -> Background work.

Every scheduled job keeps its built-in schedule and receives the time it was scheduled for. At a
slower pace, a job skips the runs that are not due: at Light (x20), a job scheduled every minute
does its work every 20 minutes. The decision depends only on that time, so every worker agrees on
it without storing anything, and a new pace applies within a minute without a restart.
"""

import time

from app.db.models import BackgroundWorkSettings
from app.db.session import session_factory

PACES = {"normal": 1, "relaxed": 5, "light": 20, "minimal": 50}
REFRESH_SECONDS = 60
_cache: dict[str, float | str] = {"pace": "normal", "read_at": float("-inf")}


def remember(pace: str) -> None:
    _cache.update(pace=pace, read_at=time.monotonic())


async def current() -> str:
    if time.monotonic() - float(_cache["read_at"]) >= REFRESH_SECONDS:
        try:
            async with session_factory()() as db:
                row = await db.get(BackgroundWorkSettings, 1)
            remember(row.pace if row and row.pace in PACES else "normal")
        except Exception:
            # Never stop scheduled work because the setting could not be read.
            remember(str(_cache["pace"]))
    return str(_cache["pace"])


def is_due(timestamp: int, every_seconds: int, pace: str) -> bool:
    factor = PACES.get(pace, 1)
    return factor == 1 or (int(timestamp) // every_seconds) % factor == 0


async def due(timestamp: int, every_seconds: int) -> bool:
    """Whether a scheduled run at this time should do its work at the current pace."""
    return is_due(timestamp, every_seconds, await current())
