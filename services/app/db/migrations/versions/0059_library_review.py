"""Keep library items Dewarr could not fully read so they can be reviewed."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0059_library_review"
down_revision = "0058_join_release_heads"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "library_assets",
        sa.Column("read_issues", JSONB(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "library_read_issues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("libraries.id"), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("title", sa.String(600)),
        sa.Column("authors", JSONB(), nullable=False, server_default="[]"),
        sa.Column("path", sa.Text()),
        sa.Column("reasons", JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("library_id", "external_id"),
    )
    op.create_index("ix_library_read_issues_library_id", "library_read_issues", ["library_id"])


def downgrade():
    op.drop_index("ix_library_read_issues_library_id", "library_read_issues")
    op.drop_table("library_read_issues")
    op.drop_column("library_assets", "read_issues")
