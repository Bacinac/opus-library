"""A person can be family without an account

Revision ID: e4b8f2a6c1d9
Revises: d7a3e5c19b42
"""
import sqlalchemy as sa
from alembic import op

revision = "e4b8f2a6c1d9"
down_revision = "d7a3e5c19b42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("photo_people", sa.Column("family", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("photo_people", "family")
