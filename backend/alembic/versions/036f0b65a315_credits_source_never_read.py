"""credits source never read

Revision ID: 036f0b65a315
Revises: 0ef7bfffd717
Create Date: 2026-09-16 15:40:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '036f0b65a315'
down_revision = '0ef7bfffd717'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('video_files', 'credits_by')


def downgrade():
    op.add_column('video_files', sa.Column('credits_by', sa.String(), server_default='', nullable=False))
