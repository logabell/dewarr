"""Persist connection check freshness and independent MAM proxy health."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0069_connection_health"
down_revision = "0068_configuration_deletion"
branch_labels = depends_on = None


def upgrade():
    for table in ("source_connections", "integrations"):
        op.add_column(table, sa.Column("last_checked_at", sa.DateTime(timezone=True)))
    op.add_column(
        "source_connections",
        sa.Column("proxy_health", postgresql.JSONB(), nullable=False, server_default="{}"),
    )


def downgrade():
    op.drop_column("source_connections", "proxy_health")
    for table in ("source_connections", "integrations"):
        op.drop_column(table, "last_checked_at")
