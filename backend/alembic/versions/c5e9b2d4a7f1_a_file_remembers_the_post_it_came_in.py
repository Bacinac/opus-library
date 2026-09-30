"""A file remembers the post it came in

Revision ID: c5e9b2d4a7f1
Revises: b3f7a1d9e6c2
"""
import sqlalchemy as sa
from alembic import op

revision = "c5e9b2d4a7f1"
down_revision = "b3f7a1d9e6c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("video_files", sa.Column("release_guid", sa.String(), nullable=False, server_default=""))
    op.add_column("video_files", sa.Column("release_title", sa.String(), nullable=False, server_default=""))
    op.execute("""
        UPDATE video_files f
        SET release_guid = d.release_guid, release_title = d.release_title
        FROM (
            SELECT DISTINCT ON ((job_ref->>'file_id')::int)
                   (job_ref->>'file_id')::int AS file_id, release_guid, release_title
            FROM video_downloads
            WHERE job_ref ? 'file_id'
            ORDER BY (job_ref->>'file_id')::int, id DESC
        ) d
        WHERE f.id = d.file_id
    """)


def downgrade() -> None:
    op.drop_column("video_files", "release_title")
    op.drop_column("video_files", "release_guid")
