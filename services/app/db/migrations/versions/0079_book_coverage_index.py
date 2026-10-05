"""Find library copies by book without scanning all asset coverage."""

from alembic import op

revision = "0079_book_coverage_index"
down_revision = "0078_recording_runtime"
branch_labels = depends_on = None


def upgrade():
    op.create_index("ix_asset_contains_work_id", "asset_contains", ["work_id"])


def downgrade():
    op.drop_index("ix_asset_contains_work_id", table_name="asset_contains")
