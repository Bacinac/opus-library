"""where a film changes scene

Revision ID: c3f8a2d51e64
Revises: a1c4e7f90b21
"""

import sqlalchemy as sa
from alembic import op

revision = "c3f8a2d51e64"
down_revision = "a1c4e7f90b21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chapters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start_s", sa.Float(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["file_id"], ["video_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chapters_file_id"), "chapters", ["file_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_chapters_file_id"), table_name="chapters")
    op.drop_table("chapters")
