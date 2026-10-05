"""Recording duration belongs to an edition, not the work."""

import sqlalchemy as sa
from alembic import op

revision = "0078_recording_runtime"
down_revision = "0077_ebook_companions"
branch_labels = depends_on = None


def upgrade():
    op.add_column("versions", sa.Column("runtime_minutes", sa.Integer(), nullable=True))
    op.create_check_constraint("version_runtime_positive", "versions", "runtime_minutes > 0")


def downgrade():
    op.drop_constraint("version_runtime_positive", "versions", type_="check")
    op.drop_column("versions", "runtime_minutes")
