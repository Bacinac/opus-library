"""a browser-ready copy of every embedded subtitle

Showing an embedded subtitle means reading it out of the container first, and
for a Blu-ray remux that is a full pass over 89 GB — measured at 183 seconds.
No player waits that long, which is why none of them ever appeared. The reading
happens once, at import, and this column says where the result is.

Revision ID: c3a51e88f2d7
Revises: b7e91d4c05af
Create Date: 2026-08-16

"""
import sqlalchemy as sa
from alembic import op

revision = 'c3a51e88f2d7'
down_revision = 'b7e91d4c05af'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('subtitles', sa.Column('vtt_path', sa.String(), nullable=True))
    # a sidecar the library fetched is already a file a browser can read
    op.execute("UPDATE subtitles SET vtt_path = path WHERE source = 'external' AND path IS NOT NULL")


def downgrade():
    op.drop_column('subtitles', 'vtt_path')
