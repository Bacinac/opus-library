"""library files table; file attributes leave tracks; release wikidata id

Revision ID: f2a7b9c1d3e5
Revises: c4d82e1f7a90
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a7b9c1d3e5"
down_revision = "c4d82e1f7a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("wikidata_id", sa.String(32), nullable=True))
    op.create_unique_constraint("uq_releases_wikidata_id", "releases", ["wikidata_id"])

    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("path", sa.Text(), nullable=False, unique=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("tag_artist", sa.String(500), nullable=True),
        sa.Column("tag_album", sa.String(500), nullable=True),
        sa.Column("tag_title", sa.String(500), nullable=True),
        sa.Column("tag_track", sa.Integer(), nullable=True),
        sa.Column("codec", sa.String(16), nullable=True),
        sa.Column("bitrate_kbps", sa.Integer(), nullable=True),
        sa.Column("sample_rate_hz", sa.Integer(), nullable=True),
        sa.Column("bit_depth", sa.Integer(), nullable=True),
        sa.Column(
            "track_id",
            sa.Integer(),
            sa.ForeignKey("tracks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_files_track_id", "files", ["track_id"])

    # DISTINCT ON: legacy data could link one file to tracks of two releases
    op.execute(
        "INSERT INTO files (path, tag_title, codec, bitrate_kbps, sample_rate_hz, "
        "bit_depth, track_id) "
        "SELECT DISTINCT ON (file_path) file_path, title, codec, bitrate_kbps, "
        "sample_rate_hz, bit_depth, id "
        "FROM tracks WHERE file_path IS NOT NULL ORDER BY file_path, id"
    )
    op.drop_column("tracks", "file_path")
    op.drop_column("tracks", "codec")
    op.drop_column("tracks", "bitrate_kbps")
    op.drop_column("tracks", "sample_rate_hz")
    op.drop_column("tracks", "bit_depth")


def downgrade() -> None:
    op.add_column("tracks", sa.Column("file_path", sa.Text(), nullable=True))
    op.add_column("tracks", sa.Column("codec", sa.String(16), nullable=True))
    op.add_column("tracks", sa.Column("bitrate_kbps", sa.Integer(), nullable=True))
    op.add_column("tracks", sa.Column("sample_rate_hz", sa.Integer(), nullable=True))
    op.add_column("tracks", sa.Column("bit_depth", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE tracks t SET file_path = f.path, codec = f.codec, "
        "bitrate_kbps = f.bitrate_kbps, sample_rate_hz = f.sample_rate_hz, "
        "bit_depth = f.bit_depth FROM files f WHERE f.track_id = t.id"
    )
    op.drop_index("ix_files_track_id", table_name="files")
    op.drop_table("files")
    op.drop_constraint("uq_releases_wikidata_id", "releases", type_="unique")
    op.drop_column("releases", "wikidata_id")
