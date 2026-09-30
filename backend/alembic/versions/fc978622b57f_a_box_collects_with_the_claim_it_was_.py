"""a box collects with the claim it was handed, and no address is assumed

The two addresses that used to be written into the code as defaults were this
household's production machines. An install that has been running is kept on
exactly the address it was reading; a fresh one starts with none.

Revision ID: fc978622b57f
Revises: 4da9ba073143
Create Date: 2026-09-16 11:32:13.083381

"""
from alembic import op
import sqlalchemy as sa


revision = 'fc978622b57f'
down_revision = '4da9ba073143'
branch_labels = None
depends_on = None

FORMER_DEFAULTS = {
    "opus_url": "http://192.168.1.103:8097",
    "dida_url": "http://192.168.1.100:5273",
}


def upgrade():
    op.add_column('devices', sa.Column('claim', sa.String(length=64), nullable=True))
    settled = op.get_bind().scalar(sa.text("SELECT count(*) FROM settings"))
    if settled:
        for key, value in FORMER_DEFAULTS.items():
            op.execute(sa.text(
                "INSERT INTO settings (key, value) VALUES (:key, :value) "
                "ON CONFLICT (key) DO NOTHING").bindparams(key=key, value=value))


def downgrade():
    op.drop_column('devices', 'claim')
