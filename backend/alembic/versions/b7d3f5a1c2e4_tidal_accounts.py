"""tidal accounts

Revision ID: b7d3f5a1c2e4
Revises: a1c5d7e9f0b2
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d3f5a1c2e4"
down_revision = "a1c5d7e9f0b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tidal_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("token_type", sa.String(32), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("expiry", sa.String(40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("tidal_accounts")
