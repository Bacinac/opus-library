"""screensaver is chosen by person, not by photograph

Revision ID: cbb4fabd5db0
Revises: 4e01b78f4915
Create Date: 2026-09-25 11:39:35.682314

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'cbb4fabd5db0'
down_revision = '4e01b78f4915'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index(op.f('ix_photo_screensaver_photo_id'), table_name='photo_screensaver')
    op.drop_table('photo_screensaver')


def downgrade():
    op.create_table('photo_screensaver',
    sa.Column('user_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('photo_id', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['photo_id'], ['photos.id'], name=op.f('photo_screensaver_photo_id_fkey'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('photo_screensaver_user_id_fkey'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'photo_id', name=op.f('photo_screensaver_pkey'))
    )
    op.create_index(op.f('ix_photo_screensaver_photo_id'), 'photo_screensaver', ['photo_id'], unique=False)
