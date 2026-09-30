"""tidal account is_pkce

Revision ID: d3d7928080c5
Revises: f2eaad06d0ca
Create Date: 2026-08-12 12:32:50.281287

"""
from alembic import op
import sqlalchemy as sa


revision = 'd3d7928080c5'
down_revision = 'f2eaad06d0ca'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tidal_accounts', sa.Column('is_pkce', sa.Boolean(), server_default='false', nullable=False))


def downgrade():
    op.drop_column('tidal_accounts', 'is_pkce')
