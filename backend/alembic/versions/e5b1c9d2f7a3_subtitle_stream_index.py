"""an embedded subtitle knows which stream it is

Demuxing a track asks ffmpeg for `-map 0:s:N`, and N was being taken from where
the row happened to sit in the list Postgres returned. Rows come back in heap
order, which is insertion order right up until something updates one — so a file
whose subtitles had been touched once handed out shifted numbers, and the track
written as the English subtitle was whatever stream N now pointed at. On Veep
S02E08 that was a Blu-ray picture track, and ffmpeg said what it says: subtitle
encoding is only possible text to text or bitmap to bitmap.

The number belongs to the row. Existing rows get it from their id order, which
IS the order they were written in.

Revision ID: e5b1c9d2f7a3
Revises: c3a51e88f2d7
Create Date: 2026-08-19

"""
import sqlalchemy as sa
from alembic import op

revision = 'e5b1c9d2f7a3'
down_revision = 'c3a51e88f2d7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('subtitles', sa.Column('stream_index', sa.Integer(), nullable=True))
    op.execute("""
        UPDATE subtitles s SET stream_index = r.rn
        FROM (SELECT id, row_number() OVER (PARTITION BY file_id ORDER BY id) - 1 AS rn
              FROM subtitles WHERE source = 'embedded') r
        WHERE s.id = r.id AND s.source = 'embedded'
    """)


def downgrade():
    op.drop_column('subtitles', 'stream_index')
