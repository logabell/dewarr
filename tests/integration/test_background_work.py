from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text

from app.config import get_settings
from app.db.models import AuditEvent
from app.jobs import pace

pytestmark = pytest.mark.integration
INDEXES = {"ix_operations_kind_status_created", "ix_operations_command_intent"}


async def test_admins_choose_the_background_work_pace(client, admin, database):
    assert (await client.get("/api/background-work")).json() == {"pace": "normal"}
    saved = await client.put("/api/background-work", json={"pace": "light"})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"pace": "light"}
    assert (await client.get("/api/background-work")).json() == {"pace": "light"}
    assert await pace.current() == "light"
    assert (await client.put("/api/background-work", json={"pace": "turbo"})).status_code == 422
    async with database() as db:
        actions = list(await db.scalars(select(AuditEvent.action)))
    assert "background_work.changed" in actions
    pace.remember("normal")


async def test_background_work_is_admin_only(client, admin):
    created = await client.post(
        "/api/auth/users",
        json={
            "username": "pace-member",
            "display_name": "Member",
            "role": "member",
            "password": "a separate long password",
        },
    )
    assert created.status_code == 201
    login = await client.post(
        "/api/auth/login",
        json={"username": "pace-member", "password": "a separate long password"},
    )
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert (await client.get("/api/background-work")).status_code == 403
    assert (await client.put("/api/background-work", json={"pace": "light"})).status_code == 403


def indexes(connection):
    return set(
        connection.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'operations'")
        ).scalars()
    )


def test_both_migrations_can_be_removed_and_restored(migrated_database):
    lookups = import_module("app.db.migrations.versions.0081_operation_lookups")
    background = import_module("app.db.migrations.versions.0082_background_work")
    engine = create_engine(get_settings().database_url.get_secret_value())
    try:
        with engine.connect() as connection, connection.begin():
            assert INDEXES <= indexes(connection)
            with Operations.context(MigrationContext.configure(connection)):
                background.downgrade()
                assert (
                    connection.execute(
                        text("SELECT to_regclass('public.background_work_settings')")
                    ).scalar()
                    is None
                )
                lookups.downgrade()
                assert not INDEXES & indexes(connection)
                lookups.upgrade()
                background.upgrade()
            assert INDEXES <= indexes(connection)
            assert connection.execute(
                text("SELECT to_regclass('public.background_work_settings')")
            ).scalar()
            # The planner can use the request index for the expression the app's queries use.
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(
                connection.execute(
                    text(
                        "EXPLAIN SELECT id FROM operations "
                        "WHERE payload['command'] ->> 'intent_id' = 'x' "
                        "AND kind = 'acquisition.auto-select'"
                    )
                ).scalars()
            )
            assert "ix_operations_command_intent" in plan
            connection.rollback()
    finally:
        engine.dispose()
