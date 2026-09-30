"""A photograph the recipe cannot read, and the index the timeline pages by.

Two files out of 41,012 fail to decode — iPhone HEICs whose HEVC configuration
libheif will not find. Until now that fact lived in a counter that a restart
cleared and a log line that rotation eats, so the pass re-read them on every
sweep and nobody could have found out. Now it is on the row, carrying the
generation that failed: a newer decoder is a new generation, and a new
generation has failed on nothing.

`ix_photos_timeline` replaces `ix_photos_taken_at`. The timeline pages by
(taken_at, id) and the composite serves both — taken_at leads it — so this
trades one index for a better one rather than adding a second.

Revision ID: 76ccea29abaf
Revises: 3589dbd88c9b
Create Date: 2026-08-25

"""
import sqlalchemy as sa
from alembic import op

revision = '76ccea29abaf'
down_revision = 'c92f4a7b1d63'
branch_labels = None
depends_on = None


def upgrade():
    # server_default, because the column is NOT NULL and the table already holds
    # 44,701 rows that have never been asked this question
    op.add_column('photos', sa.Column('derive_failed_gen', sa.Integer(),
                                      nullable=False, server_default='0'))
    op.add_column('photos', sa.Column('derive_error', sa.String(length=200),
                                      nullable=False, server_default=''))
    # created before the old one is dropped, so there is no moment in between
    # where a query that wants taken_at has nothing to read
    op.create_index('ix_photos_timeline', 'photos', ['taken_at', 'id'])
    op.drop_index('ix_photos_taken_at', table_name='photos')
    # the model has always said this is a dict; the column said it could be
    # nothing, and the drift showed up in every autogenerate since
    op.alter_column('photo_integrity_events', 'detail', nullable=False)


def downgrade():
    op.alter_column('photo_integrity_events', 'detail', nullable=True)
    op.create_index('ix_photos_taken_at', 'photos', ['taken_at'])
    op.drop_index('ix_photos_timeline', table_name='photos')
    op.drop_column('photos', 'derive_error')
    op.drop_column('photos', 'derive_failed_gen')
