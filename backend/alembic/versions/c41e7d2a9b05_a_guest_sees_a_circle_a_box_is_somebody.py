"""a guest sees a circle of people; a box may be somebody's

Revision ID: c41e7d2a9b05
Revises: abe89869ffeb
Create Date: 2026-11-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c41e7d2a9b05"
down_revision = "abe89869ffeb"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("circle", postgresql.ARRAY(sa.Integer()),
                                     server_default="{}", nullable=False))
    op.add_column("devices", sa.Column("person_id", sa.Integer(), nullable=True))
    op.create_foreign_key("devices_person_id_fkey", "devices", "users",
                          ["person_id"], ["id"], ondelete="CASCADE")


def downgrade():
    op.drop_constraint("devices_person_id_fkey", "devices", type_="foreignkey")
    op.drop_column("devices", "person_id")
    op.drop_column("users", "circle")
