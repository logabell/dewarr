"""Tracked ebook copies inside matching audiobook items."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0077_ebook_companions"
down_revision = "0076_oidc_account_linking"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "ebook_companions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("library_id", sa.Uuid(), sa.ForeignKey("libraries.id"), nullable=False),
        sa.Column("source_asset_id", sa.Uuid(), sa.ForeignKey("library_assets.id"), nullable=False),
        sa.Column("target_asset_id", sa.Uuid(), sa.ForeignKey("library_assets.id"), nullable=False),
        sa.Column("version_id", sa.Uuid(), sa.ForeignKey("versions.id"), nullable=False),
        sa.Column("source_path", sa.String(1024), nullable=False),
        sa.Column("target_path", sa.String(1024), nullable=False),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("receipt", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.UniqueConstraint("target_asset_id", "target_path"),
    )
    op.create_index("ix_ebook_companions_library_id", "ebook_companions", ["library_id"])
    op.create_index("ix_ebook_companions_target_asset_id", "ebook_companions", ["target_asset_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM ebook_companions)")):
        raise RuntimeError(
            "Ebook placement receipts must be retained; restore a backup to downgrade"
        )
    op.drop_table("ebook_companions")
