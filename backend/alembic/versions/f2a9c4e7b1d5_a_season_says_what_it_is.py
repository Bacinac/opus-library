"""a season says what it is

Revision ID: f2a9c4e7b1d5
Revises: e4b8c2d6f1a3
Create Date: 2026-09-21 18:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = "f2a9c4e7b1d5"
down_revision = "e4b8c2d6f1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("seasons", sa.Column("overview", sa.Text(), server_default="", nullable=False))
    op.add_column("seasons", sa.Column("overview_hr", sa.Text(), server_default="", nullable=False))
    op.add_column("seasons", sa.Column("poster_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("seasons", "poster_url")
    op.drop_column("seasons", "overview_hr")
    op.drop_column("seasons", "overview")
