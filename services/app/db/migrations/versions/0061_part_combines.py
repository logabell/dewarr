"""Track folding the separate part items of a recording into one library book."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0061_part_combines"
down_revision = "0060_book_parts"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "part_combines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("libraries.id"), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("versions.id"), nullable=False),
        sa.Column("work_id", sa.Uuid(), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("operation_id", sa.Uuid(), sa.ForeignKey("operations.id")),
        sa.Column("destination_id", sa.Uuid(), sa.ForeignKey("import_destinations.id")),
        sa.Column("part_asset_ids", JSONB(), nullable=False),
        sa.Column("combined_asset_id", sa.Uuid(), sa.ForeignKey("library_assets.id")),
        sa.Column("plan", JSONB()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("library_id", "version_id"),
        sa.CheckConstraint(
            "state IN ('skipped', 'combining', 'combined', 'separating', 'separated',"
            " 'needs-attention')",
            name="part_combines_state",
        ),
    )
    op.create_index("ix_part_combines_library_id", "part_combines", ["library_id"])
    op.create_index("ix_part_combines_version_id", "part_combines", ["version_id"])


def downgrade():
    op.drop_index("ix_part_combines_version_id", "part_combines")
    op.drop_index("ix_part_combines_library_id", "part_combines")
    op.drop_table("part_combines")
