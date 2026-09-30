"""the library records what is inside a file

The probe already ran a full ffprobe at import and kept a de-duplicated list of
audio LANGUAGES, dropping everything else — so a release with English TrueHD 7.1
and English stereo AC3 was recorded as the single word `en`, and anything that
wanted to choose between them had to open an 89 GB file again to find out. The
library owns what a file is; this is where it says so.

Subtitles stay in their own table: they are an acceptance criterion rather than a
property of the container, and half of them are sidecar files that are not
streams in it at all.

Revision ID: b7e91d4c05af
Revises: 135f7dbf83ed
Create Date: 2026-08-16

"""
import sqlalchemy as sa
from alembic import op

revision = 'b7e91d4c05af'
down_revision = '135f7dbf83ed'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('video_files', sa.Column('duration_s', sa.Float(), nullable=True))
    op.create_table(
        'media_streams',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('codec', sa.String(length=32), nullable=False),
        sa.Column('lang', sa.String(length=8), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('channels', sa.Integer(), nullable=True),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('bit_depth', sa.Integer(), nullable=True),
        sa.Column('color_transfer', sa.String(length=24), nullable=False),
        sa.Column('color_primaries', sa.String(length=24), nullable=False),
        sa.Column('default', sa.Boolean(), nullable=False),
        sa.Column('forced', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['file_id'], ['video_files.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_media_streams_file_id'), 'media_streams', ['file_id'],
                    unique=False)


def downgrade():
    op.drop_table('media_streams')
    op.drop_column('video_files', 'duration_s')
