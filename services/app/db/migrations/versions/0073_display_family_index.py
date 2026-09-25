"""Index candidate families for bounded book-detail grouping."""

import sqlalchemy as sa
from alembic import op

revision = "0073_display_family_index"
down_revision = "0072_inventory_observed_media"
branch_labels = depends_on = None

# Frozen to keep historical upgrades independent of future title-rule changes.
BASE_EXPRESSION = (
    "trim(split_part(trim(regexp_replace(trim(regexp_replace(translate(lower("
    "normalize(title, NFKC)), '‘’', ''''''), '\\s+', ' ', 'g')), '(?:\\s*(?::\\s"
    "*|\\(\\s*)(?:a novel|reese[''’]s book club(?: pick)?|oprah[''’]s book club"
    "(?: pick)?)[\\s)]*|\\s*\\(\\s*(?:unabridged|abridged|older version|original "
    "recording|revised edition|anniversary edition)\\s*\\)\\s*|\\s*\\(\\s*(?:read|n"
    "arrated)\\s+by\\s+([^()]+)\\)\\s*)+$', '', 'g')), ':', 1))"
)


def upgrade():
    with op.get_context().autocommit_block():
        # A cancelled concurrent build can leave an invalid index behind.
        op.drop_index(
            "ix_works_display_base",
            table_name="works",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "ix_works_display_base",
            "works",
            [sa.literal_column(BASE_EXPRESSION)],
            postgresql_concurrently=True,
        )


def downgrade():
    op.drop_index("ix_works_display_base", table_name="works")
