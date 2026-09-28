"""Native Bookdrop intake is separate from library ownership."""

import sqlalchemy as sa
from alembic import op

revision = "0075_native_import_workflows"
down_revision = "0074_destination_storage"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "import_destinations",
        sa.Column("workflow", sa.String(20), nullable=False, server_default="library"),
    )
    op.add_column(
        "import_destinations",
        sa.Column("integration_id", sa.UUID(), sa.ForeignKey("integrations.id")),
    )
    op.alter_column("import_destinations", "library_id", nullable=True)
    op.create_check_constraint(
        "destination_workflow",
        "import_destinations",
        "(workflow = 'library' AND library_id IS NOT NULL AND integration_id IS NULL) OR "
        "(workflow = 'bookdrop' AND library_id IS NULL AND integration_id IS NOT NULL "
        "AND medium = 'ebook' AND mode = 'copy' AND NOT seeding_rename)",
    )
    # The earlier unnamed constraint uses this project's naming convention.
    constraints = sa.inspect(op.get_bind()).get_check_constraints("import_entries")
    for constraint in constraints:
        if "awaiting-library" in constraint["sqltext"]:
            op.drop_constraint(op.f(constraint["name"]), "import_entries", type_="check")
    op.create_check_constraint(
        "import_entry_state",
        "import_entries",
        "state IN ('queued', 'publishing', 'awaiting-library', 'confirmed', 'held', 'skipped', "
        "'cancelling', 'cancel-held', 'cancelled', 'awaiting-review', 'needs-link', 'rejected')",
    )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM import_destinations WHERE workflow = 'bookdrop')")
    ):
        raise RuntimeError("Bookdrop receipts must be retained; restore a backup to downgrade")
    op.drop_constraint("import_entry_state", "import_entries", type_="check")
    op.create_check_constraint(
        "import_entries_state_check",
        "import_entries",
        "state IN ('queued', 'publishing', 'awaiting-library', 'confirmed', 'held', 'skipped', "
        "'cancelling', 'cancel-held', 'cancelled')",
    )
    op.drop_constraint("destination_workflow", "import_destinations", type_="check")
    op.alter_column("import_destinations", "library_id", nullable=False)
    op.drop_column("import_destinations", "integration_id")
    op.drop_column("import_destinations", "workflow")
