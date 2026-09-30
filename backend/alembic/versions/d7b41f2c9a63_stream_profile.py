"""what a sound track is beyond the name of its core

DTS and DTS-HD Master Audio are both `dca` to a probe that is only asked for the
codec; TrueHD with Atmos is `truehd`. The profile is where ffprobe says which of
them it actually is, and without it a screen can only ever show the mark of the
core — which is the wrong mark on the better copy.

Revision ID: d7b41f2c9a63
Revises: c3f5a81d9b24
Create Date: 2026-08-24

"""
import sqlalchemy as sa
from alembic import op

revision = 'd7b41f2c9a63'
down_revision = 'c3f5a81d9b24'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('media_streams', sa.Column('profile', sa.String(length=48), nullable=True))


def downgrade():
    op.drop_column('media_streams', 'profile')
