"""Record which part of a book each library item holds, and how an audio version was recorded."""

import sqlalchemy as sa
from alembic import op

revision = "0060_book_parts"
down_revision = "0059_library_review"
branch_labels = depends_on = None


def upgrade():
    op.add_column("asset_contains", sa.Column("part_index", sa.Integer()))
    op.add_column("asset_contains", sa.Column("part_total", sa.Integer()))
    op.create_check_constraint(
        "asset_contains_part",
        "asset_contains",
        "(part_index IS NULL AND part_total IS NULL)"
        " OR (part_index BETWEEN 1 AND part_total AND part_total BETWEEN 2 AND 20)",
    )
    op.add_column("versions", sa.Column("recording_kind", sa.String(20)))
    op.create_check_constraint(
        "version_recording_kind",
        "versions",
        "recording_kind IS NULL OR recording_kind IN ('narrated', 'dramatized', 'full_cast')",
    )


def downgrade():
    op.drop_constraint("version_recording_kind", "versions", type_="check")
    op.drop_column("versions", "recording_kind")
    op.drop_constraint("asset_contains_part", "asset_contains", type_="check")
    op.drop_column("asset_contains", "part_total")
    op.drop_column("asset_contains", "part_index")
