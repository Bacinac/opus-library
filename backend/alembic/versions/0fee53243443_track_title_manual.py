"""track title_manual

Revision ID: 0fee53243443
Revises: b7d3f5a1c2e4
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0fee53243443"
down_revision = "b7d3f5a1c2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tracks",
        sa.Column("title_manual", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tracks", "title_manual")
