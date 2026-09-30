"""the shelf is searched without diacritics

Revision ID: b49c5eb61c6f
Revises: f90e6a6aa828
Create Date: 2026-09-17 10:19:23.352868

"""
from alembic import op


revision = 'b49c5eb61c6f'
down_revision = 'f90e6a6aa828'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")


def downgrade():
    op.execute("DROP EXTENSION IF EXISTS unaccent")
