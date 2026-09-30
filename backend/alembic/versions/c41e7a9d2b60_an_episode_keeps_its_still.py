"""an episode keeps its still and running time

Revision ID: c41e7a9d2b60
Revises: 87b2d7c1e4a9
Create Date: 2026-09-21 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c41e7a9d2b60'
down_revision = '87b2d7c1e4a9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('episodes', sa.Column('still_url', sa.String(), nullable=True))
    op.add_column('episodes', sa.Column('runtime_min', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('episodes', 'runtime_min')
    op.drop_column('episodes', 'still_url')
