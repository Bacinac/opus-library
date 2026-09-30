"""photo screensaver choices

Revision ID: 4e01b78f4915
Revises: 05ec46a4c0a5
Create Date: 2026-09-25 10:58:16.620799

"""
from alembic import op
import sqlalchemy as sa


revision = '4e01b78f4915'
down_revision = '05ec46a4c0a5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('photo_screensaver',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('photo_id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['photo_id'], ['photos.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'photo_id')
    )
    op.create_index(op.f('ix_photo_screensaver_photo_id'), 'photo_screensaver', ['photo_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_photo_screensaver_photo_id'), table_name='photo_screensaver')
    op.drop_table('photo_screensaver')
