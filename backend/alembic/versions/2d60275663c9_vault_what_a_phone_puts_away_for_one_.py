"""vault: what a phone puts away for one person

Two tables the server cannot read. A key is stored only wrapped, twice, under a
password and under a recovery code; a file carries an encrypted blob where every
other table in this schema carries columns. What is left in the clear is whose it
is, how large it is and when it arrived — which is what a store cannot work
without.

Revision ID: 2d60275663c9
Revises: 7e9c17adca37
Create Date: 2026-08-28 10:39:03.503901

"""
from alembic import op
import sqlalchemy as sa


revision = '2d60275663c9'
down_revision = '7e9c17adca37'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('vault_keys',
    sa.Column('person', sa.String(length=64), nullable=False),
    sa.Column('salt', sa.LargeBinary(length=32), nullable=False),
    sa.Column('rounds', sa.Integer(), nullable=False),
    sa.Column('wrapped', sa.LargeBinary(), nullable=False),
    sa.Column('recovery_salt', sa.LargeBinary(length=32), nullable=False),
    sa.Column('recovery', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('person')
    )
    op.create_table('vault_files',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('person', sa.String(length=64), nullable=False),
    sa.Column('mark', sa.LargeBinary(length=32), nullable=False),
    sa.Column('bytes', sa.BigInteger(), nullable=False),
    sa.Column('at', sa.BigInteger(), nullable=False),
    sa.Column('chunk', sa.Integer(), nullable=False),
    sa.Column('keyed', sa.LargeBinary(), nullable=False),
    sa.Column('meta', sa.LargeBinary(), nullable=False),
    sa.Column('thumb', sa.LargeBinary(), nullable=True),
    sa.Column('photo_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['photo_id'], ['photos.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_vault_files_person_id', 'vault_files', ['person', 'id'], unique=False)
    op.create_index('ix_vault_files_person_mark', 'vault_files', ['person', 'mark'], unique=True)
    op.create_index(op.f('ix_vault_files_photo_id'), 'vault_files', ['photo_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_vault_files_photo_id'), table_name='vault_files')
    op.drop_index('ix_vault_files_person_mark', table_name='vault_files')
    op.drop_index('ix_vault_files_person_id', table_name='vault_files')
    op.drop_table('vault_files')
    op.drop_table('vault_keys')
