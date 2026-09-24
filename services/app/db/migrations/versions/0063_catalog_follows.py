"""Distinguish author/series follows from ordinary remote lists."""

import sqlalchemy as sa
from alembic import op

revision = "0063_catalog_follows"
down_revision = "0061_part_combines"
branch_labels = depends_on = None


def upgrade():
    op.add_column("list_subscriptions", sa.Column("source_kind", sa.String(20), nullable=True))
    op.create_check_constraint(
        "list_subscription_source_kind",
        "list_subscriptions",
        "source_kind IS NULL OR (provider = 'hardcover' AND source_kind IN ('author', 'series'))",
    )


def downgrade():
    op.drop_constraint("list_subscription_source_kind", "list_subscriptions", type_="check")
    op.drop_column("list_subscriptions", "source_kind")
