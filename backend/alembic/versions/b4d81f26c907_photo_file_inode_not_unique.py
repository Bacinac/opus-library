"""an inode is unique on a disk, not in a catalogue that remembers

photo_files also holds rows for files that are gone, and the filesystem hands
their inode numbers to something else once they are. A unique constraint on
(dev, inode) therefore refuses the new photograph in order to protect a stale
row — exactly backwards. The pairing is still indexed, because the move lookup
reads it on every file of every pass; what makes that lookup safe is that it
also requires the size and mtime to agree, not that the number is unique.

Revision ID: b4d81f26c907
Revises: a1c7e3b95d40
Create Date: 2026-08-25

"""
from alembic import op

revision = 'b4d81f26c907'
down_revision = 'a1c7e3b95d40'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('uq_photo_files_inode', 'photo_files', type_='unique')
    op.create_index('ix_photo_files_inode', 'photo_files', ['dev', 'inode'])


def downgrade():
    op.drop_index('ix_photo_files_inode', table_name='photo_files')
    op.create_unique_constraint('uq_photo_files_inode', 'photo_files', ['dev', 'inode'])
