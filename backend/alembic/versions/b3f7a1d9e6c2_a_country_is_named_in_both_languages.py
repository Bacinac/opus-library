"""a country is named in both languages

Revision ID: b3f7a1d9e6c2
Revises: a8d2e5f1c3b7
Create Date: 2026-09-21 21:30:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "b3f7a1d9e6c2"
down_revision = "a8d2e5f1c3b7"
branch_labels = None
depends_on = None

# Wikidata's English and Croatian labels for every country the library had
# stored, some under one name and some under the other.
NAMES = {
    "Australia": "Australija",
    "Bosnia and Herzegovina": "Bosna i Hercegovina",
    "Canada": "Kanada",
    "Croatia": "Hrvatska",
    "France": "Francuska",
    "Ireland": "Irska",
    "Italy": "Italija",
    "Jamaica": "Jamajka",
    "Mexico": "Meksiko",
    "Montenegro": "Crna Gora",
    "North Macedonia": "Sjeverna Makedonija",
    "Norway": "Norveška",
    "Serbia": "Srbija",
    "Slovenia": "Slovenija",
    "Socialist Federal Republic of Yugoslavia": "Socijalistička Federativna Republika Jugoslavija",
    "Spain": "Španjolska",
    "Sweden": "Švedska",
    "Switzerland": "Švicarska",
    "United Kingdom": "Ujedinjeno Kraljevstvo",
    "United States": "Sjedinjene Američke Države",
    "West Germany": "Zapadna Njemačka",
}


def upgrade() -> None:
    op.add_column("artists", sa.Column("country_hr", sa.String(64), nullable=True))
    bind = op.get_bind()
    for en, hr in NAMES.items():
        bind.execute(
            text("UPDATE artists SET country = :en, country_hr = :hr WHERE country IN (:en, :hr)"),
            {"en": en, "hr": hr},
        )
    left = bind.execute(text(
        "SELECT DISTINCT country FROM artists WHERE country IS NOT NULL AND country_hr IS NULL"
    )).scalars().all()
    if left:
        raise RuntimeError(f"countries without a Croatian name: {left}")


def downgrade() -> None:
    op.drop_column("artists", "country_hr")
