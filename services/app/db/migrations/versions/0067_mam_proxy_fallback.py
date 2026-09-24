"""Allow a configured source proxy to fall back to a direct route."""

import sqlalchemy as sa
from alembic import op

revision = "0067_mam_proxy_fallback"
down_revision = "0066_notifications_follows"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "source_connections",
        sa.Column(
            "proxy_fallback_direct",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM source_connections "
            "WHERE proxy_url IS NOT NULL AND proxy_fallback_direct = true)"
        )
    ):
        raise RuntimeError(
            "Restore a pre-upgrade backup rather than discarding source fallback policy"
        )
    op.drop_column("source_connections", "proxy_fallback_direct")
