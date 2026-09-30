"""a dead post outlives its queue row

Revision ID: 05ec46a4c0a5
Revises: 7f0a008bf737
Create Date: 2026-09-25 11:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '05ec46a4c0a5'
down_revision = '7f0a008bf737'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('dead_posts',
    sa.Column('guid', sa.String(), nullable=False),
    sa.Column('release_title', sa.String(), nullable=False),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.Column('died_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('guid')
    )
    op.execute("""
        INSERT INTO dead_posts (guid, release_title, detail, died_at)
        SELECT DISTINCT ON (release_guid) release_guid, release_title, detail, updated_at
        FROM video_downloads
        WHERE state = 'failed' AND release_guid <> '' AND detail <> 'imported file row is gone'
        ORDER BY release_guid, updated_at
    """)


def downgrade():
    op.drop_table('dead_posts')
