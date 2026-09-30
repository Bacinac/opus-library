"""index tracks.release_id and releases.artist_id

Postgres indexes primary keys and unique constraints and nothing else: a foreign
key column stays unindexed until somebody says so. These two were, and they are
the two every read of the music library walks — an album's tracks, an artist's
albums — so 61k track rows were scanned to answer a question about one album.

Revision ID: 135f7dbf83ed
Revises: f4c8b2a17e90
Create Date: 2026-08-15 22:08:27.359867

"""
from alembic import op


revision = '135f7dbf83ed'
down_revision = 'f4c8b2a17e90'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(op.f('ix_releases_artist_id'), 'releases', ['artist_id'], unique=False)
    op.create_index(op.f('ix_tracks_release_id'), 'tracks', ['release_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_tracks_release_id'), table_name='tracks')
    op.drop_index(op.f('ix_releases_artist_id'), table_name='releases')
