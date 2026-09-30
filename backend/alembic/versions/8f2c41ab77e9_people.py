"""Who is in the photographs.

person ──< cluster ──< face. The machine sees a likeness, a human sees a person,
and the two are different questions — so the name lives on the person, a cluster
is only a grouping that may be rebuilt, and a face carries the human's answer so
that rebuilding one costs nothing.

Revision ID: 8f2c41ab77e9
Revises: 76ccea29abaf
Create Date: 2026-08-25

"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC

revision = '8f2c41ab77e9'
down_revision = '76ccea29abaf'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    op.create_table(
        'photo_people',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('name', sa.String(120), nullable=False, unique=True),
        sa.Column('born_on', sa.Date, nullable=True),
        sa.Column('cover_face_id', sa.Integer, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        'photo_face_clusters',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('person_id', sa.Integer,
                  sa.ForeignKey('photo_people.id', ondelete='SET NULL'), nullable=True),
        sa.Column('generation', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_photo_face_clusters_person_id', 'photo_face_clusters', ['person_id'])
    op.create_index('ix_photo_face_clusters_generation', 'photo_face_clusters', ['generation'])

    op.create_table(
        'photo_faces',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('photo_id', sa.Integer,
                  sa.ForeignKey('photos.id', ondelete='CASCADE'), nullable=False),
        sa.Column('cluster_id', sa.Integer,
                  sa.ForeignKey('photo_face_clusters.id', ondelete='SET NULL'), nullable=True),
        sa.Column('person_id', sa.Integer,
                  sa.ForeignKey('photo_people.id', ondelete='SET NULL'), nullable=True),
        sa.Column('x', sa.Float, nullable=False),
        sa.Column('y', sa.Float, nullable=False),
        sa.Column('w', sa.Float, nullable=False),
        sa.Column('h', sa.Float, nullable=False),
        sa.Column('score', sa.Float, nullable=False, server_default='0'),
        sa.Column('embedding', HALFVEC(512), nullable=False),
        sa.Column('generation', sa.Integer, nullable=False, server_default='0'),
        sa.Column('found_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_photo_faces_photo_id', 'photo_faces', ['photo_id'])
    op.create_index('ix_photo_faces_cluster_id', 'photo_faces', ['cluster_id'])
    op.create_index('ix_photo_faces_person_id', 'photo_faces', ['person_id'])
    op.create_index('ix_photo_faces_generation', 'photo_faces', ['generation'])

    # The cover is a plain pointer added after both tables exist, because the two
    # reference each other and one of them has to be second.
    op.create_foreign_key('fk_photo_people_cover_face', 'photo_people',
                          'photo_faces', ['cover_face_id'], ['id'], ondelete='SET NULL')

    # Cosine and not L2: these vectors come out of the model normalised, so the
    # two orders agree — and cosine keeps agreeing if a later model does not
    # normalise, which is the cheaper mistake to have already avoided.
    op.create_index('ix_photo_faces_embedding', 'photo_faces', ['embedding'],
                    postgresql_using='hnsw',
                    postgresql_ops={'embedding': 'halfvec_cosine_ops'})


def downgrade():
    op.drop_index('ix_photo_faces_embedding', table_name='photo_faces')
    op.drop_constraint('fk_photo_people_cover_face', 'photo_people', type_='foreignkey')
    op.drop_table('photo_faces')
    op.drop_table('photo_face_clusters')
    op.drop_table('photo_people')
