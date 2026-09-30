"""who made it

Revision ID: a4d7e2c81b93
Revises: f8c31e5a70d2
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a4d7e2c81b93"
down_revision = "f8c31e5a70d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("movies", "series"):
        op.add_column(table, sa.Column("studios", JSONB(), nullable=False,
                                       server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    for table in ("movies", "series"):
        op.drop_column(table, "studios")
