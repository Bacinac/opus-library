"""a music file knows when it arrived

Revision ID: d7a3f1c9e2b4
Revises: c41e7a9d2b60
Create Date: 2026-09-21 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd7a3f1c9e2b4'
down_revision = 'c41e7a9d2b60'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('music_files', sa.Column(
        'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
        nullable=False))
    # every row already here would otherwise have arrived today; the file's own
    # date is the nearest thing to when the house got it
    op.execute("UPDATE music_files SET created_at = to_timestamp(mtime) WHERE mtime IS NOT NULL")
    op.create_index('ix_music_files_created_at', 'music_files', ['created_at'])


def downgrade():
    op.drop_index('ix_music_files_created_at', table_name='music_files')
    op.drop_column('music_files', 'created_at')
