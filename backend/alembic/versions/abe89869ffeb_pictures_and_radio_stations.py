"""pictures and radio stations

Revision ID: abe89869ffeb
Revises: b974239eaff3
Create Date: 2026-10-30 18:05:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "abe89869ffeb"
down_revision = "b974239eaff3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pictures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=8), server_default="wanted", nullable=False),
        sa.Column("file", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("tries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source"),
    )
    op.create_index(op.f("ix_pictures_state"), "pictures", ["state"], unique=False)
    op.create_table(
        "radio_stations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("genre", sa.String(length=200), server_default="", nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("logo", sa.Text(), nullable=True),
        sa.Column("country", sa.String(length=8), server_default="", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("radio_stations")
    op.drop_index(op.f("ix_pictures_state"), table_name="pictures")
    op.drop_table("pictures")
