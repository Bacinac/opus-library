"""files mtime

Revision ID: 0a5191bf1d24
Revises: 0fee53243443
Create Date: 2026-08-12 09:32:23.560392

"""
from alembic import op
import sqlalchemy as sa


revision = '0a5191bf1d24'
down_revision = '0fee53243443'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('files', sa.Column('mtime', sa.Float(), nullable=True))


def downgrade():
    op.drop_column('files', 'mtime')
