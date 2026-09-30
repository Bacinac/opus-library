"""wiki cache external ids

Revision ID: 03a6523c1121
Revises: 02ceaf602f56
Create Date: 2026-08-12 10:53:29.392748

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '03a6523c1121'
down_revision = '02ceaf602f56'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('wiki_album_cache',
                  sa.Column('external_ids', postgresql.JSONB(astext_type=sa.Text()),
                            nullable=True))


def downgrade():
    op.drop_column('wiki_album_cache', 'external_ids')
