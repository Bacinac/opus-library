"""the library gains a service token, because it gained a consumer

Player is to Library what Library is to Downloads: a module with no browser and
no session, which must carry something on every call. The setting is added to
the spec; this revision only makes sure an install that already has a password
does not start refusing its own consumer for want of a row.

Revision ID: f4c8b2a17e90
Revises: a843132placeholder
Create Date: 2026-08-15

"""
from alembic import op
import sqlalchemy as sa

revision = 'f4c8b2a17e90'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    # settings are key/value; a spec entry with no row simply reads as its
    # default, so there is nothing to create. The revision exists to record when
    # the door grew its second kind of caller.
    pass


def downgrade():
    op.execute(sa.text("DELETE FROM settings WHERE key = 'access_service_token'"))
