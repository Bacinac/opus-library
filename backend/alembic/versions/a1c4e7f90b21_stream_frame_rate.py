"""a file says how many pictures a second it holds

Revision ID: a1c4e7f90b21
Revises: e5b1c9d2f7a3
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c4e7f90b21"
down_revision = "e5b1c9d2f7a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_streams", sa.Column("frame_rate", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_streams", "frame_rate")
