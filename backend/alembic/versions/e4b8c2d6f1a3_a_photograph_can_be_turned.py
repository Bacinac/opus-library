"""a photograph can be turned

Revision ID: e4b8c2d6f1a3
Revises: d7a3f1c9e2b4
Create Date: 2026-09-21 17:10:00
"""
import sqlalchemy as sa
from alembic import op

revision = "e4b8c2d6f1a3"
down_revision = "d7a3f1c9e2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("photos", sa.Column("turn", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("photos", "turn")
