"""
Indexes for the most frequent operation lookups. Background jobs and request pages look operations
up by kind and status, and by the request id inside the command payload; without these indexes,
each lookup read the whole table. The request counts went from about 4 minutes to under a second.
"""

import sqlalchemy as sa
from alembic import op

revision = "0081_operation_lookups"
down_revision = "0080_member_setup"
branch_labels = depends_on = None


def upgrade():
    op.create_index(
        "ix_operations_kind_status_created",
        "operations",
        ["kind", "status", "created_at"],
    )
    op.create_index(
        "ix_operations_command_intent",
        "operations",
        # Written with subscripts, exactly as the app's queries compile, or Postgres ignores it. Not
        # a partial index: Postgres cannot tell that an equality on this value implies the key
        # exists, so a partial index would never be used.
        [sa.text("(payload['command'] ->> 'intent_id')"), "kind", "created_at"],
    )


def downgrade():
    op.drop_index("ix_operations_command_intent", table_name="operations")
    op.drop_index("ix_operations_kind_status_created", table_name="operations")
