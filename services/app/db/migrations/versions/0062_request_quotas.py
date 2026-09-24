"""Durable request quota policies and admissions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0062_request_quotas"
down_revision = "0061_part_combines"
branch_labels = depends_on = None


def upgrade():
    op.add_column("acquisition_targets", sa.Column("quota_requirement", JSONB()))
    op.create_table(
        "request_quota_policies",
        sa.Column("scope", sa.String(100), primary_key=True),
        sa.Column("configuration", JSONB(), nullable=False),
    )
    op.create_table(
        "request_quota_charges",
        sa.Column(
            "target_id",
            sa.Uuid(),
            sa.ForeignKey("acquisition_targets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("medium", sa.String(10), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("size_at", sa.DateTime(timezone=True)),
        sa.Column("exempt", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_request_quota_charges_owner_id", "request_quota_charges", ["owner_id"])
    op.create_index(
        "ix_request_quota_charges_admitted_at", "request_quota_charges", ["admitted_at"]
    )
    op.add_column(
        "acquisition_targets",
        sa.Column("quota_waiting", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("acquisition_targets", sa.Column("quota_retry_at", sa.DateTime(timezone=True)))

    # Preserve the original rolling timestamps when upgrading an existing instance.
    # Existing-library satisfaction and another target's committed transfer are free.
    op.execute("""
        INSERT INTO request_quota_charges
            (target_id, owner_id, medium, admitted_at, size_bytes, size_at, exempt)
        SELECT t.id, i.owner_id,
            CASE WHEN t.slot = 'either' THEN i.specification->>'preferred_medium' ELSE t.slot END,
            i.created_at, 0, NULL, false
        FROM acquisition_targets t
        JOIN acquisition_intents i ON i.id = t.intent_id
        JOIN users u ON u.id = i.owner_id
        WHERE u.role <> 'admin'
          AND (t.state <> 'satisfied' OR EXISTS (
              SELECT 1 FROM acquisition_selections s WHERE s.target_id = t.id))
          AND NOT EXISTS (SELECT 1 FROM acquisition_selections s
              WHERE s.reservation_id = t.reservation_id AND s.target_id <> t.id
                AND s.state IN ('committed', 'fulfilled'))
    """)


def downgrade():
    op.drop_column("acquisition_targets", "quota_requirement")
    op.drop_column("acquisition_targets", "quota_retry_at")
    op.drop_column("acquisition_targets", "quota_waiting")
    op.drop_table("request_quota_charges")
    op.drop_table("request_quota_policies")
