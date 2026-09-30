"""files duration_sec

Revision ID: eb668d0ebf87
Revises: d3d7928080c5
Create Date: 2026-08-12 21:20:09.262231

"""
from alembic import op
import sqlalchemy as sa


revision = 'eb668d0ebf87'
down_revision = 'd3d7928080c5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('files', sa.Column('duration_sec', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('files', 'duration_sec')
