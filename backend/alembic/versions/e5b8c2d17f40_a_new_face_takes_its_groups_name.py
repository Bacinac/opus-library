"""a new face takes its group's name, and a face remembers whom it was taken off

Revision ID: e5b8c2d17f40
Revises: d7a3f1c08e52
Create Date: 2026-11-06 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "e5b8c2d17f40"
down_revision = "d7a3f1c08e52"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("photo_faces", sa.Column(
        "not_person_id", sa.Integer(),
        sa.ForeignKey("photo_people.id", ondelete="SET NULL"), nullable=True))
    op.execute("""
        UPDATE photo_faces f SET person_id = c.person_id
        FROM photo_face_clusters c, photo_people pp, photos p
        WHERE f.cluster_id = c.id AND pp.id = c.person_id AND p.id = f.photo_id
          AND f.person_id IS NULL
          AND 1 - (f.embedding <=> c.centroid) >= 0.62
          AND (pp.born_on IS NULL OR p.taken_at IS NULL OR p.taken_at::date >= pp.born_on)
    """)


def downgrade():
    op.drop_column("photo_faces", "not_person_id")
