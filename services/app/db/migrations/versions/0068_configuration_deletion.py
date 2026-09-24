"""Retire configuration while retaining download and library history."""

import sqlalchemy as sa
from alembic import op

revision = "0068_configuration_deletion"
down_revision = "0067_mam_proxy_fallback"
branch_labels = depends_on = None


def upgrade():
    for table in ("integrations", "source_connections", "import_destinations"):
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    for table in ("import_destinations", "source_connections", "integrations"):
        op.drop_column(table, "deleted_at")
