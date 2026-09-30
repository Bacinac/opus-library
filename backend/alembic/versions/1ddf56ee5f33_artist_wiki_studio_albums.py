"""artist wiki studio albums

Revision ID: 1ddf56ee5f33
Revises: 03a6523c1121
Create Date: 2026-08-12 11:08:29.914788

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '1ddf56ee5f33'
down_revision = '03a6523c1121'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('artists',
                  sa.Column('wiki_studio_albums',
                            postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade():
    op.drop_column('artists', 'wiki_studio_albums')
