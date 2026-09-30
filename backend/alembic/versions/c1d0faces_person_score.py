"""the face index carries the score, so a person's best face is a read

The people list opens with a portrait per person, chosen as the highest-scoring
face they have. On the person alone that is a sort of every face they are in —
four and a half thousand of them for the most photographed — done once per
person, every time the screen opens. With the score in the key it is the first
row of an index scan.

Revision ID: c1d0faces
Revises: bf75d52baae3
Create Date: 2026-08-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'c1d0faces'
down_revision = 'bf75d52baae3'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index('ix_photo_faces_person_id', table_name='photo_faces')
    op.create_index('ix_photo_faces_person_id', 'photo_faces',
                    ['person_id', sa.literal_column('score DESC')],
                    unique=False, postgresql_include=['photo_id'])


def downgrade():
    op.drop_index('ix_photo_faces_person_id', table_name='photo_faces')
    op.create_index('ix_photo_faces_person_id', 'photo_faces', ['person_id'],
                    unique=False, postgresql_include=['photo_id', 'score'])
