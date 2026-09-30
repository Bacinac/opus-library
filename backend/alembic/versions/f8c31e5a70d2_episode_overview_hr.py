"""the Croatian overview of an episode

Revision ID: f8c31e5a70d2
Revises: e7a2d94c1b38
"""
import sqlalchemy as sa
from alembic import op

revision = "f8c31e5a70d2"
down_revision = "e7a2d94c1b38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("episodes", sa.Column("overview_hr", sa.Text(), nullable=False,
                                        server_default=""))


def downgrade() -> None:
    op.drop_column("episodes", "overview_hr")
