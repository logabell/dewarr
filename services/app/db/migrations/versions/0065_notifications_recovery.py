"""Join notifications with the quotas and download-recovery migration history."""

revision = "0065_notifications_recovery"
down_revision = ("0062_notifications", "0064_quotas_recovery")
branch_labels = depends_on = None


def upgrade():
    pass


def downgrade():
    pass
