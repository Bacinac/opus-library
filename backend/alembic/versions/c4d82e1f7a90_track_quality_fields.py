"""track quality fields

Revision ID: c4d82e1f7a90
Revises: 9f31c7a0d5e4
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d82e1f7a90"
down_revision = "9f31c7a0d5e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tracks", sa.Column("codec", sa.String(16), nullable=True))
    op.add_column("tracks", sa.Column("bitrate_kbps", sa.Integer(), nullable=True))
    op.add_column("tracks", sa.Column("sample_rate_hz", sa.Integer(), nullable=True))
    op.add_column("tracks", sa.Column("bit_depth", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tracks", "bit_depth")
    op.drop_column("tracks", "sample_rate_hz")
    op.drop_column("tracks", "bitrate_kbps")
    op.drop_column("tracks", "codec")
