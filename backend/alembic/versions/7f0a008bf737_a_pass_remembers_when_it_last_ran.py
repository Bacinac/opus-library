"""a pass remembers when it last ran

Revision ID: 7f0a008bf737
Revises: 801e45d4292e
Create Date: 2026-09-25 00:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '7f0a008bf737'
down_revision = '801e45d4292e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('loop_passes',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )


def downgrade():
    op.drop_table('loop_passes')
