"""download rejected cancelled

Revision ID: 07a2f4f638f1
Revises: c4a169ae0ec6
Create Date: 2026-08-14 16:26:13.748349

"""
from alembic import op


revision = '07a2f4f638f1'
down_revision = 'c4a169ae0ec6'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TYPE downloadstatus ADD VALUE IF NOT EXISTS 'rejected'")
    op.execute("ALTER TYPE downloadstatus ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade():
    # postgres cannot drop a value from an enum type
    pass
