"""what a record is besides its name: its label and what kind of music it is

The catalogue already held the keys to every source — deezer, discogs, spotify,
wikidata — and none of what they say about a record beyond its tracklist. The
sleeve could name the file's codec and not the label that put the record out.

Empty means asked and there was nothing, the same convention the description
uses, so a record is not a request on every view for ever.

Revision ID: c3f5a81d9b24
Revises: a1c7e3b9d240
Create Date: 2026-08-24

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'c3f5a81d9b24'
down_revision = 'a1c7e3b9d240'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('releases', sa.Column('label', sa.String(length=200), nullable=True))
    op.add_column('releases', sa.Column('genres', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('releases', 'genres')
    op.drop_column('releases', 'label')
