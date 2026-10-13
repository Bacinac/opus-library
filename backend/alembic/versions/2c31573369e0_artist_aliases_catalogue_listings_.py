from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "2c31573369e0"
down_revision = "17d461f4b468"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_listings",
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("artist_key", sa.String(length=128), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("source", "artist_key"),
    )
    op.execute(
        "INSERT INTO catalog_listings (source, artist_key, items, fetched_at) "
        "SELECT 'discogs', artist_id::text, masters, fetched_at FROM discogs_masters_cache")
    op.drop_table("discogs_masters_cache")
    op.create_table(
        "deezer_album_credits",
        sa.Column("album_id", sa.BigInteger(), nullable=False),
        sa.Column("principal_id", sa.BigInteger(), nullable=True),
        sa.Column("principal_name", sa.String(length=500), nullable=False),
        sa.Column("main_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("album_id"),
    )
    op.create_table(
        "source_cooldowns",
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source"),
    )
    op.create_table(
        "artist_aliases",
        sa.Column("artist_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["artist_id"], ["artists.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("artist_id", "alias"),
    )


def downgrade():
    op.drop_table("artist_aliases")
    op.drop_table("source_cooldowns")
    op.drop_table("deezer_album_credits")
    op.create_table(
        "discogs_masters_cache",
        sa.Column("artist_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("masters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("artist_id"),
    )
    op.execute(
        "INSERT INTO discogs_masters_cache (artist_id, masters, fetched_at) "
        "SELECT artist_key::bigint, items, fetched_at FROM catalog_listings "
        "WHERE source = 'discogs'")
    op.drop_table("catalog_listings")
