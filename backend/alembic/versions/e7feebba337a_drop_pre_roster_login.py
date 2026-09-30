"""the login from before the roster is gone

access_username and access_password were the one login the install had before
people had a table of their own. Nothing reads them, and one of them is a
password kept in the clear.

Revision ID: e7feebba337a
Revises: 7772da022279
Create Date: 2026-09-23

"""
from alembic import op
import sqlalchemy as sa

revision = 'e7feebba337a'
down_revision = '7772da022279'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("DELETE FROM settings WHERE key IN ('access_username', 'access_password')"))


def downgrade():
    pass
