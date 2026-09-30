"""release description

Revision ID: a1c5d7e9f0b2
Revises: f2a7b9c1d3e5
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c5d7e9f0b2"
down_revision = "f2a7b9c1d3e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("releases", "description")
