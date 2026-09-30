"""the same overview in Croatian, where TMDB has one

Revision ID: e7a2d94c1b38
Revises: d5b1c8a34f70
"""
import sqlalchemy as sa
from alembic import op

revision = "e7a2d94c1b38"
down_revision = "d5b1c8a34f70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("movies", "series"):
        op.add_column(table, sa.Column("overview_hr", sa.Text(), nullable=False,
                                       server_default=""))


def downgrade() -> None:
    for table in ("movies", "series"):
        op.drop_column(table, "overview_hr")
