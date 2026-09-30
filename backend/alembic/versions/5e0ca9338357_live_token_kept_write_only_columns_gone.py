"""live token kept, write-only columns gone

Revision ID: 5e0ca9338357
Revises: fc978622b57f
Create Date: 2026-09-16 11:46:20.246214

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '5e0ca9338357'
down_revision = 'fc978622b57f'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('photo_files', 'seen_at')
    op.add_column('photos', sa.Column('live_token', sa.String(length=64), nullable=True))
    op.create_index('ix_photos_live_token', 'photos', ['live_token'], unique=False, postgresql_where=sa.text("live_token <> ''"))
    op.drop_column('tidal_accounts', 'is_pkce')
    op.drop_column('tidal_accounts', 'token_type')
    op.drop_column('tidal_accounts', 'access_token')
    op.drop_column('tidal_accounts', 'expiry')

    # a followed artist's new record was marked complete before its tracklist
    # was ever fetched; failed is what the monitor asks about again
    op.execute("""
        UPDATE releases SET status = 'failed'
        WHERE status = 'complete'
          AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.release_id = releases.id)
    """)
    # faces moved out of a group by hand never moved its centroid
    op.execute("""
        UPDATE photo_face_clusters c
        SET centroid = fresh.v::halfvec, faces_at = fresh.n
        FROM (SELECT f.cluster_id AS id, count(*) AS n, AVG(f.embedding::vector) AS v
              FROM photo_faces f WHERE f.cluster_id IS NOT NULL
              GROUP BY f.cluster_id) fresh
        WHERE fresh.id = c.id
    """)


def downgrade():
    op.add_column('tidal_accounts', sa.Column('expiry', sa.VARCHAR(length=40), nullable=True))
    op.add_column('tidal_accounts', sa.Column('access_token', sa.TEXT(), nullable=True))
    op.add_column('tidal_accounts', sa.Column('token_type', sa.VARCHAR(length=32), nullable=True))
    op.add_column('tidal_accounts', sa.Column('is_pkce', sa.BOOLEAN(), server_default=sa.text('false'), nullable=False))
    op.drop_index('ix_photos_live_token', table_name='photos', postgresql_where=sa.text("live_token <> ''"))
    op.drop_column('photos', 'live_token')
    op.add_column('photo_files', sa.Column('seen_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False))
