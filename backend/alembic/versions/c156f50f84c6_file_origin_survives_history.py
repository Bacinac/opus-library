"""file origin survives history

Revision ID: c156f50f84c6
Revises: 07a2f4f638f1
Create Date: 2026-08-14 17:11:57.525732

"""
from alembic import op


revision = 'c156f50f84c6'
down_revision = '07a2f4f638f1'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f('files_download_id_fkey'), 'files', type_='foreignkey')


def downgrade():
    op.create_foreign_key(op.f('files_download_id_fkey'), 'files', 'downloads',
                          ['download_id'], ['id'], ondelete='SET NULL')
