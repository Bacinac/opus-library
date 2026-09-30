"""two indexes the people and places screens read through

The people list aggregates every face in the library. `photo_faces` is four
hundred megabytes because each row carries an embedding, so the plan reads the
whole heap to fetch two narrow columns; carrying them in the index turns that
into four megabytes of index-only scan.

The places list asks, once per place, which photograph taken there most recently
has a picture made of it. On the name alone that is a sort per place; with the
date in the index it is a read of the first row.

Revision ID: bf75d52baae3
Revises: 03aca1e4e3ff
Create Date: 2026-08-28 14:57:50.627033

"""
from alembic import op
import sqlalchemy as sa


revision = 'bf75d52baae3'
down_revision = '03aca1e4e3ff'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_photos_place_time', 'photos',
                    ['place', 'country', sa.literal_column('taken_at DESC NULLS LAST')],
                    unique=False)
    # the same index by the same name, now carrying what the aggregate reads.
    # Autogenerate does not see an INCLUDE change, so the swap is written out.
    op.drop_index('ix_photo_faces_person_id', table_name='photo_faces')
    op.create_index('ix_photo_faces_person_id', 'photo_faces', ['person_id'],
                    unique=False, postgresql_include=['photo_id', 'score'])


def downgrade():
    op.drop_index('ix_photos_place_time', table_name='photos')
    op.drop_index('ix_photo_faces_person_id', table_name='photo_faces')
    op.create_index('ix_photo_faces_person_id', 'photo_faces', ['person_id'],
                    unique=False)
