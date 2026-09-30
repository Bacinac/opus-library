"""A hollow subtitle track is not read again

Revision ID: d7a3e5c19b42
Revises: c5e9b2d4a7f1
"""
import sqlalchemy as sa
from alembic import op

revision = "d7a3e5c19b42"
down_revision = "c5e9b2d4a7f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("subtitles", sa.Column("hollow", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("subtitles", "hollow")
