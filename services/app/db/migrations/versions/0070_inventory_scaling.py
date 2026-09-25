"""Bounded inventory membership, verification, and revision reuse."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0070_inventory_scaling"
down_revision = "0069_connection_health"
branch_labels = depends_on = None


def upgrade():
    op.add_column("inventory_observations", sa.Column("source_marker", postgresql.JSONB()))
    op.add_column(
        "inventory_observations",
        sa.Column("verified", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "inventory_observations",
        sa.Column("reused", sa.Boolean(), nullable=False, server_default="false"),
    )
    # Older interrupted runs can contain an unvalidated cross-library duplicate.
    # Do not alter their evidence; new runs check global membership before writes.
    op.create_index(
        "ix_inventory_observation_membership",
        "inventory_observations",
        ["run_id", "item_external_id"],
    )
    op.create_table(
        "inventory_item_states",
        sa.Column(
            "integration_id",
            sa.UUID(),
            sa.ForeignKey("integrations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("library_external_id", sa.String(200), primary_key=True),
        sa.Column("item_external_id", sa.String(200), primary_key=True),
        sa.Column("source_marker", postgresql.JSONB(), nullable=False),
        sa.Column("scope_fingerprint", sa.String(64), nullable=False),
        sa.Column("credential_generation", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "inventory_absences",
        sa.Column(
            "run_id",
            sa.UUID(),
            sa.ForeignKey("inventory_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "asset_id",
            sa.UUID(),
            sa.ForeignKey("library_assets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("missing_since", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("inventory_absences")
    op.drop_table("inventory_item_states")
    op.drop_index("ix_inventory_observation_membership", table_name="inventory_observations")
    op.drop_column("inventory_observations", "reused")
    op.drop_column("inventory_observations", "verified")
    op.drop_column("inventory_observations", "source_marker")
