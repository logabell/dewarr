"""Durable failed-download replacement and work-scoped release exclusions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0063_download_recovery"
down_revision = "0061_part_combines"
branch_labels = depends_on = None


def identity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def fk(name, table, *, nullable=False, unique=False):
    return sa.Column(
        name, sa.Uuid(), sa.ForeignKey(table + ".id"), nullable=nullable, unique=unique
    )


def upgrade():
    op.add_column("download_attempts", sa.Column("recovery_observation", pg.JSONB()))
    op.create_table(
        "download_recovery_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("configuration", pg.JSONB(), nullable=False),
        sa.CheckConstraint("id = 1"),
    )
    op.create_table(
        "release_blocks",
        *identity(),
        fk("work_id", "works"),
        sa.Column("medium", sa.String(10), nullable=False),
        sa.Column("release_key", sa.String(64), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("identities", pg.JSONB(), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        fk("actor_id", "users"),
        sa.Column("automatic", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("work_id", "medium", "release_key"),
    )
    op.create_index("ix_release_blocks_work_id", "release_blocks", ["work_id"])
    op.create_table(
        "download_recoveries",
        *identity(),
        fk("selection_id", "acquisition_selections", unique=True),
        fk("attempt_id", "download_attempts"),
        fk("root_selection_id", "acquisition_selections"),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("evidence", pg.JSONB(), nullable=False),
        fk("search_id", "operations", nullable=True),
        fk("replacement_id", "operations", nullable=True),
        sa.Column("job_id", sa.BigInteger()),
    )
    op.create_index("ix_download_recoveries_attempt_id", "download_recoveries", ["attempt_id"])
    op.create_index("ix_download_recoveries_state", "download_recoveries", ["state"])
    op.create_table(
        "reported_download_assets",
        *identity(),
        fk("owner_id", "users"),
        fk("asset_id", "library_assets"),
        fk("recovery_id", "download_recoveries"),
        sa.UniqueConstraint("owner_id", "asset_id"),
    )


def downgrade():
    op.drop_table("reported_download_assets")
    op.drop_table("download_recoveries")
    op.drop_table("release_blocks")
    op.drop_table("download_recovery_settings")
    op.drop_column("download_attempts", "recovery_observation")
