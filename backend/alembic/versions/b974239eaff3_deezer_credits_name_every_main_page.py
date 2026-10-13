"""deezer credits name every main page

Revision ID: b974239eaff3
Revises: 2c31573369e0
Create Date: 2026-10-13 13:38:26.115593

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b974239eaff3"
down_revision = "2c31573369e0"
branch_labels = None
depends_on = None


def _replace(old: str, new: str) -> None:
    # A cache: the names were never stored, so every album is read again.
    op.execute("DELETE FROM deezer_album_credits")
    op.drop_column("deezer_album_credits", old)
    op.add_column("deezer_album_credits",
                  sa.Column(new, postgresql.JSONB(astext_type=sa.Text()), nullable=False))


def upgrade():
    _replace("main_ids", "mains")


def downgrade():
    _replace("mains", "main_ids")
