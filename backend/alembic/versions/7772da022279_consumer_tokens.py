"""each consuming module carries a token of its own

The one service token becomes one per consumer. Each starts as the value the
consumer already holds, so nothing is turned away on upgrade; an admin issues
fresh ones from the account page and carries each to its module.

Revision ID: 7772da022279
Revises: e4b8f2a6c1d9
Create Date: 2026-09-23

"""
from alembic import op
import sqlalchemy as sa

revision = '7772da022279'
down_revision = 'e4b8f2a6c1d9'
branch_labels = None
depends_on = None

CONSUMERS = ("player", "downloads", "cameras")


def upgrade():
    for name in CONSUMERS:
        op.execute(sa.text(
            "INSERT INTO settings (key, value) SELECT :key, value FROM settings "
            "WHERE key = 'access_service_token'").bindparams(key=f"access_{name}_token"))
    op.execute(sa.text("DELETE FROM settings WHERE key = 'access_service_token'"))


def downgrade():
    op.execute(sa.text(
        "INSERT INTO settings (key, value) SELECT 'access_service_token', value FROM settings "
        "WHERE key = 'access_player_token'"))
    op.execute(sa.text("DELETE FROM settings WHERE key IN "
                       "('access_player_token', 'access_downloads_token', 'access_cameras_token')"))
