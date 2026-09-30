"""discogs masters cache

Revision ID: 02ceaf602f56
Revises: b5a50887c00b
Create Date: 2026-08-12 10:34:40.136431

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '02ceaf602f56'
down_revision = 'b5a50887c00b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'discogs_masters_cache',
        sa.Column('artist_id', sa.BigInteger(), nullable=False),
        sa.Column('masters', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('artist_id'),
    )


def downgrade():
    op.drop_table('discogs_masters_cache')
