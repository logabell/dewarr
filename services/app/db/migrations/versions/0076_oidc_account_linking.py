"""Single-use, session-bound OIDC account linking."""

import sqlalchemy as sa
from alembic import op

revision = "0076_oidc_account_linking"
down_revision = "0075_native_import_workflows"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "oidc_link_attempts",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column(
            "session_hash",
            sa.String(64),
            sa.ForeignKey("login_sessions.token_hash", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(300), nullable=False),
        sa.Column("client_id", sa.String(200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oidc_link_attempts_session_hash", "oidc_link_attempts", ["session_hash"])


def downgrade():
    op.drop_table("oidc_link_attempts")
