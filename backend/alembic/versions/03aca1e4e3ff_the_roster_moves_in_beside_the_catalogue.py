"""the roster moves in beside the catalogue

It was in Downloads because Downloads happened to answer /auth/verify first —
the module you can switch off without anybody noticing until something new fails
to arrive. It belongs with the thing that has to be running for anything to work
at all, and beside the hundred and twenty-three faces the library already knows
by name.

The table is created empty and the rows are carried across afterwards, from one
database to another, hashes and all — nobody has to choose a new password.

Revision ID: 03aca1e4e3ff
Revises: 601178d2c6ae
Create Date: 2026-08-28 13:47:47.442403

"""
from alembic import op
import sqlalchemy as sa


revision = '03aca1e4e3ff'
down_revision = '601178d2c6ae'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('display', sa.String(length=120), nullable=False, server_default=''),
    sa.Column('secret', sa.Text(), nullable=False),
    sa.Column('owner', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('disabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('person_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['photo_people.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name'),
    sa.UniqueConstraint('person_id')
    )


def downgrade():
    op.drop_table('users')
