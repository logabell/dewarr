import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.db.migrations import index_reflection  # noqa: F401
from app.db.models import Base

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("alter_index", [False, True])
def test_migrated_schema_matches_models_and_real_index_drift_is_detected(
    migrated_database, alter_index
):
    engine = create_engine(get_settings().database_url.get_secret_value())
    try:
        with engine.connect() as connection, connection.begin():
            if alter_index:
                connection.execute(text("DROP INDEX ix_works_display_base"))
                connection.execute(text("CREATE INDEX ix_works_display_base ON works (title)"))
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_name": lambda name, kind, parents: (
                        not name.startswith("procrastinate_") if kind == "table" else True
                    )
                },
            )
            differences = compare_metadata(context, Base.metadata)
            if alter_index:
                assert {operation[0] for operation in differences} == {"remove_index", "add_index"}
                assert all(
                    operation[1].name == "ix_works_display_base" for operation in differences
                )
            else:
                assert differences == []
            connection.rollback()
    finally:
        engine.dispose()
