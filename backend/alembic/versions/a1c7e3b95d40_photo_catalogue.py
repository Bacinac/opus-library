"""a catalogue over photographs that are never moved

The other two halves name what they import and put it on a shelf. A photograph
was on the shelf before this application existed, so this half only describes
what is already there — which is why a file row carries an inode.

Two tables where one would do, because a photograph and a path are different
facts: the same picture is often on the disk twice, and addressing it by content
makes that one photograph with two files rather than two photographs that match.

`photo_files` carries dev, inode, size and mtime so that a storage template
rewriting 45,000 paths at once costs one lstat per file instead of a full
rehash — and so that ext4 recycling an inode number cannot silently re-point a
row, with its names and albums, at a different photograph.

Revision ID: a1c7e3b95d40
Revises: d7b41f2c9a63
Create Date: 2026-08-25

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'a1c7e3b95d40'
down_revision = 'd7b41f2c9a63'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'photos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('checksum', sa.LargeBinary(length=20), nullable=False),
        sa.Column('byte_size', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('kind', sa.String(length=8), nullable=False, server_default='image'),
        sa.Column('taken_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('taken_offset', sa.String(length=8), nullable=False, server_default=''),
        sa.Column('taken_source', sa.String(length=10), nullable=False, server_default='none'),
        sa.Column('device_make', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('device_model', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('pixel_w', sa.Integer(), nullable=True),
        sa.Column('pixel_h', sa.Integer(), nullable=True),
        sa.Column('live_pair_id', sa.Integer(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['live_pair_id'], ['photos.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_photos_checksum', 'photos', ['checksum'], unique=True)
    op.create_index('ix_photos_live_pair_id', 'photos', ['live_pair_id'])
    # the timeline reads this and nothing else, oldest photograph first
    op.create_index('ix_photos_taken_at', 'photos', ['taken_at'])

    op.create_table(
        'photo_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('photo_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('dev', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('inode', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('byte_size', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('mtime_ns', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('state', sa.String(length=12), nullable=False, server_default='present'),
        sa.Column('missing_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('seen_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['photo_id'], ['photos.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dev', 'inode', name='uq_photo_files_inode'),
    )
    op.create_index('ix_photo_files_path', 'photo_files', ['path'], unique=True)
    op.create_index('ix_photo_files_photo_id', 'photo_files', ['photo_id'])
    op.create_index('ix_photo_files_state', 'photo_files', ['state'])

    op.create_table(
        'photo_integrity_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('path', sa.String(), nullable=False, server_default=''),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_photo_integrity_events_at', 'photo_integrity_events', ['at'])
    op.create_index('ix_photo_integrity_events_kind', 'photo_integrity_events', ['kind'])


def downgrade():
    op.drop_table('photo_integrity_events')
    op.drop_table('photo_files')
    op.drop_table('photos')
