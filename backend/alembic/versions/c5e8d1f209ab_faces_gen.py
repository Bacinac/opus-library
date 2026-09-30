"""A photograph with nobody in it is an answer, not an omission.

Revision ID: c5e8d1f209ab
Revises: 8f2c41ab77e9
Create Date: 2026-08-26

"""
import sqlalchemy as sa
from alembic import op

revision = 'c5e8d1f209ab'
down_revision = '8f2c41ab77e9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('photos', sa.Column('faces_gen', sa.Integer(), nullable=False,
                                      server_default='0'))
    op.create_index('ix_photos_faces_gen', 'photos', ['faces_gen'])


def downgrade():
    op.drop_index('ix_photos_faces_gen', table_name='photos')
    op.drop_column('photos', 'faces_gen')
