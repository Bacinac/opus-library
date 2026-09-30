"""twenty-one bytes in the row, everything else on disk

A thumbnail is not state, it is the result of a computation over a file we
already hold: delete it, come back in four hours, and it is identical to the
byte. So the tile and the preview live on disk outside every backup, and the
database keeps only what is too small to cost anything — a thumbhash, which
draws a blurred impression of the picture in twenty-one bytes and therefore
travels with the query that lists it.

The measurement behind that split: shared_buffers on this install is 128 MB and
the full derivative set is 46x larger. Pushing 194 MiB of blobs through it took
the TOAST relation to 97 % of the buffer pool and evicted the photo heap and its
date index to zero cached pages — about 1,150 preview fetches, two minutes of one
person swiping an album, flushes the working set of the whole application. There
is no setting that fixes it; the blobs are simply bigger than the cache.

`derived_gen` is what makes a change of recipe a number rather than a sweep.

Revision ID: c92f4a7b1d63
Revises: b4d81f26c907
Create Date: 2026-08-25

"""
import sqlalchemy as sa
from alembic import op

revision = 'c92f4a7b1d63'
down_revision = 'b4d81f26c907'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('photos', sa.Column('thumbhash', sa.LargeBinary(length=32), nullable=True))
    op.add_column('photos', sa.Column('derived_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('photos', sa.Column('derived_gen', sa.Integer(), nullable=False,
                                      server_default='0'))
    # the derivation pass asks for what is not current, and asks it 44,701 times
    op.create_index('ix_photos_derived_gen', 'photos', ['derived_gen'])


def downgrade():
    op.drop_index('ix_photos_derived_gen', table_name='photos')
    op.drop_column('photos', 'derived_gen')
    op.drop_column('photos', 'derived_at')
    op.drop_column('photos', 'thumbhash')
