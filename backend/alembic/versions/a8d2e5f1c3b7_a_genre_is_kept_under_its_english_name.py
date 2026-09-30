"""a genre is kept under its English name

Revision ID: a8d2e5f1c3b7
Revises: f2a9c4e7b1d5
Create Date: 2026-09-21 21:00:00
"""
import json

from alembic import op
from sqlalchemy import text

revision = "a8d2e5f1c3b7"
down_revision = "f2a9c4e7b1d5"
branch_labels = None
depends_on = None

# Deezer's Croatian names for the genres the library had been given in
# Croatian, against the English name the same Deezer genre id carries.
ENGLISH = {
    "Alternativna glazba": "Alternative",
    "Elektronička glazba": "Electro",
    "Džez": "Jazz",
    "Vokalni džez": "Vocal jazz",
    "Međunarodna pop glazba": "International Pop",
    "Rock i Roll/Rockabilly": "Rock & Roll/Rockabilly",
    "Klasična glazba": "Classical",
    "Filmovi/Igrice": "Films/Games",
    "Filmska glazba": "Film Scores",
    "Latino": "Latin Music",
    "Dječje": "Kids",
}


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(text(
        "SELECT id, genres FROM releases WHERE genres ?| :names"
    ), {"names": list(ENGLISH)}).all()
    for release_id, genres in rows:
        bind.execute(
            text("UPDATE releases SET genres = CAST(:genres AS jsonb) WHERE id = :id"),
            {"id": release_id, "genres": json.dumps([ENGLISH.get(g, g) for g in genres])},
        )


def downgrade() -> None:
    pass
