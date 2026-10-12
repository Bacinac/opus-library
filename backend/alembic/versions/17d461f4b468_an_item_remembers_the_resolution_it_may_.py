from alembic import op
import sqlalchemy as sa


revision = "17d461f4b468"
down_revision = "2fa41939b5a2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("movies", sa.Column("upgrade_to", sa.String(), nullable=True))
    op.add_column("episodes", sa.Column("upgrade_to", sa.String(), nullable=True))


def downgrade():
    op.drop_column("episodes", "upgrade_to")
    op.drop_column("movies", "upgrade_to")
