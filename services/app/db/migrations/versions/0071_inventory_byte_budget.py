"""Byte-bounded inventory apply and indexed display-title identity lookup."""

import sqlalchemy as sa
from alembic import op

revision = "0071_inventory_byte_budget"
down_revision = "0070_inventory_scaling"
branch_labels = depends_on = None

# Frozen normalization expression matching catalog_titles.display_title_sql.
TITLE_EXPRESSION = (
    "trim(regexp_replace(trim(regexp_replace(translate(lower(normaliz"
    "e(title, NFKC)), '‘’', ''''''), '\\s+', ' ', 'g')), '(?:\\s*(?::\\s"
    "*|\\(\\s*)(?:a novel|reese[''’]s book club(?: pick)?|oprah[''’]s b"
    "ook club(?: pick)?)[\\s)]*|\\s*\\(\\s*(?:unabridged|abridged|older v"
    "ersion|original recording|revised edition|anniversary edition)\\s"
    "*\\)\\s*|\\s*\\(\\s*(?:read|narrated)\\s+by\\s+([^()]+)\\)\\s*)+$', '', '"
    "g'))"
)


def upgrade():
    op.execute(
        "ALTER TABLE inventory_observations "
        "ADD COLUMN IF NOT EXISTS snapshot_bytes integer NOT NULL DEFAULT 0"
    )
    # New runs populate the estimate. Interrupted older runs are discarded before
    # publication by the inventory lease owner.
    with op.get_context().autocommit_block():
        # A concurrent build can be interrupted before Alembic records this
        # revision. Rebuild a leftover (possibly invalid) index on retry.
        op.drop_index(
            "ix_works_display_title",
            table_name="works",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "ix_works_display_title",
            "works",
            [sa.literal_column(TITLE_EXPRESSION)],
            postgresql_concurrently=True,
        )


def downgrade():
    # Keep downgrades transactional: a lower revision may reject removal of
    # populated import history, which must also restore this index and column.
    op.drop_index("ix_works_display_title", table_name="works")
    op.drop_column("inventory_observations", "snapshot_bytes")
