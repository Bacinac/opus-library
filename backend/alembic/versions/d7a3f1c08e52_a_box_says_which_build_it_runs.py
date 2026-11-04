"""a box says which build of the app it runs

Revision ID: d7a3f1c08e52
Revises: c41e7d2a9b05
Create Date: 2026-11-04 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "d7a3f1c08e52"
down_revision = "c41e7d2a9b05"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("devices", sa.Column("app", sa.String(length=32),
                                       server_default="", nullable=False))


def downgrade():
    op.drop_column("devices", "app")
