"""who offered a photograph, and where an offer lands

Provenance against the checksum rather than the path, because the catalogue is
content-addressed and the tree will be reshaped one day. The note is read once,
as the picture enters the catalogue, and thrown away.

Revision ID: 601178d2c6ae
Revises: 2d60275663c9
Create Date: 2026-08-28 10:55:00.818078

"""
from alembic import op
import sqlalchemy as sa


revision = '601178d2c6ae'
down_revision = '2d60275663c9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('photo_offers',
    sa.Column('checksum', sa.LargeBinary(length=20), nullable=False),
    sa.Column('person', sa.String(length=64), nullable=False),
    sa.Column('vault_id', sa.String(length=32), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('checksum')
    )
    op.add_column('photos', sa.Column('offered_by', sa.String(length=64), nullable=True))


def downgrade():
    op.drop_column('photos', 'offered_by')
    op.drop_table('photo_offers')
