"""wiki album cache

Revision ID: b5a50887c00b
Revises: 0a5191bf1d24
Create Date: 2026-08-12 10:27:38.184243

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b5a50887c00b'
down_revision = '0a5191bf1d24'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'wiki_album_cache',
        sa.Column('qid', sa.String(length=32), nullable=False),
        sa.Column('record_type', sa.String(length=16), nullable=True),
        sa.Column('tracklist', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('qid'),
    )


def downgrade():
    op.drop_table('wiki_album_cache')
