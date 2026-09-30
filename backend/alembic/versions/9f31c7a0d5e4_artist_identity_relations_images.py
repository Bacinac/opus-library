"""artist identity, relations, external ids, images

Revision ID: 9f31c7a0d5e4
Revises: 272e4ee2c716
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "9f31c7a0d5e4"
down_revision = "272e4ee2c716"
branch_labels = None
depends_on = None

enrichstatus = sa.Enum("pending", "resolved", "unresolved", "failed", name="enrichstatus")


def upgrade() -> None:
    enrichstatus.create(op.get_bind(), checkfirst=True)

    op.add_column("artists", sa.Column("wikidata_id", sa.String(32), nullable=True))
    op.add_column("artists", sa.Column("artist_type", sa.String(16), nullable=True))
    op.add_column("artists", sa.Column("country", sa.String(64), nullable=True))
    op.add_column("artists", sa.Column("begin_year", sa.Integer(), nullable=True))
    op.add_column("artists", sa.Column("end_year", sa.Integer(), nullable=True))
    op.add_column("artists", sa.Column("bio", sa.Text(), nullable=True))
    op.add_column("artists", sa.Column("bio_url", sa.Text(), nullable=True))
    op.add_column(
        "artists",
        sa.Column("monitored", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "artists",
        sa.Column("enrich_status", enrichstatus, nullable=False, server_default="pending"),
    )
    op.create_unique_constraint("uq_artists_wikidata_id", "artists", ["wikidata_id"])

    op.create_table(
        "artist_external_ids",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "artist_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.UniqueConstraint("source", "external_id"),
    )

    op.create_table(
        "artist_relations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "artist_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "related_artist_id",
            sa.Integer(),
            sa.ForeignKey("artists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.UniqueConstraint("artist_id", "related_artist_id", "relation"),
    )

    op.create_table(
        "images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("chosen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("entity_type", "entity_id", "url"),
    )

    # consolidate the per-column external ids into the new table, then drop them
    op.execute(
        "INSERT INTO artist_external_ids (artist_id, source, external_id) "
        "SELECT id, 'spotify', spotify_id FROM artists WHERE spotify_id IS NOT NULL"
    )
    op.execute(
        "INSERT INTO artist_external_ids (artist_id, source, external_id) "
        "SELECT id, 'discogs', discogs_id::text FROM artists WHERE discogs_id IS NOT NULL"
    )
    op.drop_column("artists", "spotify_id")
    op.drop_column("artists", "discogs_id")


def downgrade() -> None:
    op.add_column("artists", sa.Column("spotify_id", sa.String(64), nullable=True))
    op.add_column("artists", sa.Column("discogs_id", sa.BigInteger(), nullable=True))
    op.execute(
        "UPDATE artists a SET spotify_id = e.external_id FROM artist_external_ids e "
        "WHERE e.artist_id = a.id AND e.source = 'spotify'"
    )
    op.execute(
        "UPDATE artists a SET discogs_id = e.external_id::bigint FROM artist_external_ids e "
        "WHERE e.artist_id = a.id AND e.source = 'discogs'"
    )
    op.drop_table("images")
    op.drop_table("artist_relations")
    op.drop_table("artist_external_ids")
    op.drop_constraint("uq_artists_wikidata_id", "artists", type_="unique")
    op.drop_column("artists", "enrich_status")
    op.drop_column("artists", "monitored")
    op.drop_column("artists", "bio_url")
    op.drop_column("artists", "bio")
    op.drop_column("artists", "end_year")
    op.drop_column("artists", "begin_year")
    op.drop_column("artists", "country")
    op.drop_column("artists", "artist_type")
    op.drop_column("artists", "wikidata_id")
    enrichstatus.drop(op.get_bind(), checkfirst=True)
