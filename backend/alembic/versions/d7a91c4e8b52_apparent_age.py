"""How old a face looks, which is not who it is.

Revision ID: d7a91c4e8b52
Revises: c5e8d1f209ab
Create Date: 2026-08-26

"""
import sqlalchemy as sa
from alembic import op

revision = 'd7a91c4e8b52'
down_revision = 'c5e8d1f209ab'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('photo_faces', sa.Column('apparent_age', sa.Float(), nullable=True))


def downgrade():
    op.drop_column('photo_faces', 'apparent_age')
