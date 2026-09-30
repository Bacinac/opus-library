"""directors and studios are named AND numbered

A name is what a reader sees; the id is what the rest of their work is found
by. Emptying both columns is what makes the next metadata pass refill them in
the new shape — nothing is lost, TMDB holds all of it.

Revision ID: b6e9f14a2c07
Revises: a4d7e2c81b93
"""
from alembic import op

revision = "b6e9f14a2c07"
down_revision = "a4d7e2c81b93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("movies", "series"):
        op.execute(f"UPDATE {table} SET directors = '[]'::jsonb, studios = '[]'::jsonb")


def downgrade() -> None:
    for table in ("movies", "series"):
        op.execute(f"UPDATE {table} SET directors = '[]'::jsonb, studios = '[]'::jsonb")
