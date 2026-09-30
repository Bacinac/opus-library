"""the words to a track, and when each line is sung

A rip carries lyrics about a third of the time and never with timings, and the
importer treats the library as read-only — so words that arrive from elsewhere
are kept beside the track rather than written into the file. A miss is a row
too: an instrumental must not send the next play back to the network.

Revision ID: a1c7e3b9d240
Revises: b6e9f14a2c07
Create Date: 2026-08-22

"""
import sqlalchemy as sa
from alembic import op

revision = 'a1c7e3b9d240'
down_revision = 'b6e9f14a2c07'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'track_lyrics',
        sa.Column('track_id', sa.Integer(), nullable=False),
        sa.Column('synced', sa.Text(), nullable=True),
        sa.Column('plain', sa.Text(), nullable=True),
        sa.Column('instrumental', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('looked_at', sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(['track_id'], ['tracks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('track_id'),
    )


def downgrade():
    op.drop_table('track_lyrics')
