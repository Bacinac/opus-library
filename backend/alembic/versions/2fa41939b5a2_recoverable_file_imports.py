from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "2fa41939b5a2"
down_revision = "f0591744e5c4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("file_imports",
                    sa.Column("id", sa.String(32), primary_key=True),
                    sa.Column("entries", postgresql.JSONB(), nullable=False),
                    sa.Column("committed", sa.Boolean(), nullable=False))


def downgrade():
    op.drop_table("file_imports")
