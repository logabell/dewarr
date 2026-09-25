"""Preserve the formats established by cached inventory observations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0072_inventory_observed_media"
down_revision = "0071_inventory_byte_budget"
branch_labels = depends_on = None


def upgrade():
    # Legacy revision-cache rows are refreshed by inventory schema version 2.
    op.add_column(
        "inventory_item_states",
        sa.Column("observed_media", postgresql.JSONB(), nullable=False, server_default="[]"),
    )


def downgrade():
    op.drop_column("inventory_item_states", "observed_media")
