"""genres, directors and cast on movies and series

Revision ID: d5b1c8a34f70
Revises: c3f8a2d51e64
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d5b1c8a34f70"
down_revision = "c3f8a2d51e64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("movies", "series"):
        for column in ("genres", "directors", "cast"):
            op.add_column(table, sa.Column(column, JSONB(), nullable=False,
                                           server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    for table in ("movies", "series"):
        for column in ("genres", "directors", "cast"):
            op.drop_column(table, column)
