"""the shelf is searched through an index

A search unaccented every title and name on every row of the joined shelf,
three times per track; trigram indexes answer it from the three tables alone.

Revision ID: 9a863d4293bd
Revises: e7feebba337a
Create Date: 2026-09-24

"""
from alembic import op


revision = '9a863d4293bd'
down_revision = 'e7feebba337a'
branch_labels = None
depends_on = None

SEARCHED = (("artists", "name"), ("releases", "title"), ("tracks", "title"))


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # unaccent() is STABLE because it finds its dictionary through search_path;
    # naming the dictionary is the same function made IMMUTABLE, which an index needs
    op.execute("CREATE FUNCTION unaccented(text) RETURNS text "
               "LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT "
               "RETURN public.unaccent('public.unaccent'::regdictionary, $1)")
    for table, column in SEARCHED:
        op.execute(f"CREATE INDEX ix_{table}_{column}_unaccented ON {table} "
                   f"USING gin (unaccented({column}) gin_trgm_ops)")


def downgrade():
    for table, column in SEARCHED:
        op.drop_index(f"ix_{table}_{column}_unaccented", table_name=table)
    op.execute("DROP FUNCTION unaccented(text)")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
