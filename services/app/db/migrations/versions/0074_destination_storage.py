"""Independent destination staging, retaining legacy journal locations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0074_destination_storage"
down_revision = "0073_display_family_index"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "import_storage_settings",
        sa.Column("storage_routes", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    # Existing global staging remains the fallback, so frozen specifications and
    # route fingerprints stay valid without moving a byte of recovery evidence.


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM import_storage_settings "
            "WHERE storage_routes <> '{}'::jsonb)"
        )
    ):
        raise RuntimeError(
            "Independent storage routes cannot be downgraded without losing recovery bindings"
        )
    op.drop_column("import_storage_settings", "storage_routes")
