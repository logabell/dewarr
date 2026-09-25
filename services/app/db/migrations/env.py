import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.migrations import index_reflection  # noqa: F401
from app.db.models import Base

config = context.config


def run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_name=lambda name, type_, parents: (
            not name.startswith("procrastinate_") if type_ == "table" else True
        ),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    engine = create_async_engine(get_settings().database_url.get_secret_value())
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url.get_secret_value(),
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
