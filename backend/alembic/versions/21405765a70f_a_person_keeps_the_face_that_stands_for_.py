"""a person keeps the face that stands for them

Revision ID: 21405765a70f
Revises: b49c5eb61c6f
Create Date: 2026-09-17 11:18:53.907421

"""
from alembic import op
import sqlalchemy as sa


revision = '21405765a70f'
down_revision = 'b49c5eb61c6f'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('photo_people', sa.Column('cover_face_id', sa.Integer(), nullable=True))
    op.create_foreign_key('photo_people_cover_face_id_fkey', 'photo_people', 'photo_faces',
                          ['cover_face_id'], ['id'], ondelete='SET NULL')


def downgrade():
    op.drop_constraint('photo_people_cover_face_id_fkey', 'photo_people', type_='foreignkey')
    op.drop_column('photo_people', 'cover_face_id')
