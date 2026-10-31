from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=2,
        hide_parameters=True,
        # JIT compiling the large status queries cost more than it saved (17 s on one count).
        connect_args={"options": "-csearch_path=public,book_queue -cjit=off"},
    )


def session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def database() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as session:
        yield session
