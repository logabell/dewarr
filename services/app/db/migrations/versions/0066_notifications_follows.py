"""Join notification and author/series follow migration histories."""

revision = "0066_notifications_follows"
down_revision = ("0065_notifications_recovery", "0065_follows_recovery")
branch_labels = depends_on = None


def upgrade():
    pass


def downgrade():
    pass
