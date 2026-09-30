"""a person may sign in with a passkey

Revision ID: f0591744e5c4
Revises: cbb4fabd5db0
Create Date: 2026-09-25 17:28:39.993396

"""
from alembic import op
import sqlalchemy as sa


revision = 'f0591744e5c4'
down_revision = 'cbb4fabd5db0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('passkeys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('credential', sa.LargeBinary(), nullable=False),
    sa.Column('public_key', sa.LargeBinary(), nullable=False),
    sa.Column('sign_count', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('credential')
    )
    op.create_index(op.f('ix_passkeys_user_id'), 'passkeys', ['user_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_passkeys_user_id'), table_name='passkeys')
    op.drop_table('passkeys')
