"""
How often background jobs run. One install-wide row; "normal" keeps every job on its built-in
schedule, and the slower paces skip runs to save CPU and database work on small servers.
"""

import sqlalchemy as sa
from alembic import op

revision = "0082_background_work"
down_revision = "0081_operation_lookups"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "background_work_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pace", sa.String(length=20), nullable=False, server_default="normal"),
        sa.CheckConstraint("id = 1", name="background_work_settings_id_check"),
        sa.CheckConstraint(
            "pace IN ('normal', 'relaxed', 'light', 'minimal')",
            name="background_work_settings_pace_check",
        ),
    )


def downgrade():
    op.drop_table("background_work_settings")
