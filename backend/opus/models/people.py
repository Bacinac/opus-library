"""Who is in the photographs.

Three tables for what looks like one idea, because the machine and the person
are not answering the same question. The machine sees a **likeness** — faces
that resemble one another — and is good at it. It will never know that the baby
at a christening and the girl at her graduation have the same owner, and it
should not be asked: that is a question about a life, not about pixels. A person
sees the **person**, and knows it at a glance.

    person ────< cluster ────< face ──── photograph
    Jana         2003–2007      box + vector
                 2008–2013
                 2019–2025

Naming attaches a *cluster* to a person, so two clusters of the same person is
one gesture rather than five thousand. Periods come free: a cluster has a span
because its faces have dates, so "Jana 2008–2013" is a consequence and not a
field.

The reason a face carries its own `person_id` as well as its cluster is the
whole point of the design. Re-clustering is a thing that has to be allowed to
happen — a better model, more photographs, a bad grouping — and it produces new
clusters. If the name lived only on the cluster it would die with it, which is
exactly Immich's documented behaviour: its remedy for bad clustering erases
every name assigned. Here a human's judgement about a face is written on the
face, so a new cluster is handed the person its faces already belong to, and
nothing a person decided is ever undone by a machine changing its mind.

The vectors are halfvec: 16 bits a dimension rather than 32. On 60,000 faces
that is 62 MB against 124 MB and the recall difference is not measurable at this
scale — the distances being compared are between different people's faces, not
between neighbouring bits.
"""

import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import text as sa_text
from sqlalchemy import (ARRAY, Boolean, Date, DateTime, Float, ForeignKey, Index,
                        Integer, String, func, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opus.models.base import Base

# The dimensionality every face model this library has considered produces.
# Written down rather than inferred so a model that does not agree fails at the
# column instead of halfway through a pass.
FACE_DIMS = 512

# Bump when the detector or the embedder changes. Same idea as the derivative
# generation: a change of model is a number, and a face from an older generation
# is visible as one rather than silently compared against vectors that mean
# something else.
FACE_GENERATION = 1

# How many faces a group needs before it is compared with other groups at all.
# Below it a group is dust: it says nothing about who somebody is, and a group
# somebody has NAMED is never dust whatever its size. Written here rather than
# passed in, because the index that makes the comparison affordable is built on
# this exact condition and the two must not drift.
COMPARED = 8


class Person(Base):
    """Somebody. The only row in this file a human writes."""

    __tablename__ = "photo_people"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Given and family name kept apart, because they are asked different
    # questions: a household is sorted and grouped by the family name and
    # addressed by the given one. The full name is what they make together and
    # is stored as well, because it is what a person types and what has to stay
    # unique — deriving it would mean deriving the thing being searched.
    given_name: Mapped[str] = mapped_column(String(80), default="")
    family_name: Mapped[str] = mapped_column(String(80), default="", index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # for the five people in this household who have one. Not decoration: an
    # apparent age against a capture date is what separates two sisters who look
    # alike at the same age, which is the case pure arithmetic cannot do.
    born_on: Mapped[datetime.date | None] = mapped_column(Date)
    # Where the date came from. A date carried in from a contacts book is not
    # ours to edit — the book is where it is kept and this is a copy of it, and a
    # field that can be changed in two places is two facts waiting to disagree.
    born_source: Mapped[str] = mapped_column(String(16), default="")
    # Family the house has no account for — a grandmother, the cousins. Whoever
    # signs in here is family already through the roster; this is for the
    # people a wall shown unasked may show besides them.
    family: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"))
    # what the contacts book calls this person, so a rename there still finds
    # them here and a second import does not create a stranger
    contact_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    # the face a wall of people shows for them, chosen by photos.people.covers
    cover_face_id: Mapped[int | None] = mapped_column(
        ForeignKey("photo_faces.id", ondelete="SET NULL", use_alter=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class FaceCluster(Base):
    """A likeness the machine found: faces that resemble one another.

    Rebuilt whenever the clustering is run again. It holds no name — it holds a
    pointer to whoever its faces turned out to belong to, and losing that pointer
    costs nothing because the faces still carry the answer."""

    __tablename__ = "photo_face_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("photo_people.id", ondelete="SET NULL"))
    # The average of its faces, kept rather than recomputed. It changes only when
    # its faces do, and whatever moves a face out of a group writes it again;
    # recomputing it on every request cost more than the question being asked:
    # a cross join over a thousand groups is a million distances, while the same
    # question against a stored, indexed centroid is a thousand index lookups.
    centroid: Mapped[list[float] | None] = mapped_column(HALFVEC(FACE_DIMS))
    # how many faces the stored centroid was made from
    faces_at: Mapped[int | None] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())

    faces: Mapped[list["Face"]] = relationship(back_populates="cluster")

    __table_args__ = (
        # the run view asks every group who else looks like it; without this the
        # question is a cross join and a million distances
        # Partial, and that is the whole point. Fourteen thousand groups have a
        # centroid and a thousand of them are ever compared; an index over all of
        # them spends every search wading through dust. Over the thousand it is
        # four times faster — 262 ms against 1.13 s for the question the run view
        # asks on every refresh.
        Index("ix_photo_face_clusters_compared", "centroid",
              postgresql_using="hnsw",
              postgresql_ops={"centroid": "halfvec_cosine_ops"},
              postgresql_where=text(
                  f"centroid IS NOT NULL AND "
                  f"(faces_at >= {COMPARED} OR person_id IS NOT NULL)")),
        # How many groups belong to a person, asked once per person every time
        # the people screen opens. It existed on the database and not in the
        # model, which is how the next autogenerate came to offer to drop it —
        # and dropped, that answer becomes a scan of every group in the library.
        Index("ix_photo_face_clusters_person_id", "person_id"),
    )


class Face(Base):
    """One face in one photograph, and where it sits in the vector space.

    The box is stored as fractions of the frame rather than pixels, so it stays
    correct against the tile, the preview and the original — three sizes of the
    same picture, and a face that has to be re-measured for each of them is a
    face that will be wrong in two of them."""

    __tablename__ = "photo_faces"
    __table_args__ = (
        # the pass asks "which faces are not of this generation", 60,000 times
        Index("ix_photo_faces_generation", "generation"),
        # Declared here and not only in the migration. An index the model does
        # not know about is one the next autogenerate offers to drop, and this
        # is the index every "who else looks like this" question goes through —
        # dropped, the answer becomes a scan of every face in the library.
        Index("ix_photo_faces_embedding", "embedding",
              postgresql_using="hnsw",
              postgresql_ops={"embedding": "halfvec_cosine_ops"}),
        # The people list aggregates every face in the library, so the plan is a
        # scan whatever the index — and this table is four hundred megabytes
        # because each row carries an embedding. Carrying the two columns that
        # question actually reads turns four hundred megabytes of heap into four
        # of index, which is the whole of the difference between a screen that
        # opens and one that thinks about it.
        Index("ix_photo_faces_person_id", "person_id",
              sa_text("score DESC"), postgresql_include=["photo_id"]),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), index=True)
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("photo_face_clusters.id", ondelete="SET NULL"), index=True)
    # What a human decided about THIS face, and the reason re-clustering is safe.
    # Set when somebody names the cluster it was in; never cleared by a machine.
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("photo_people.id", ondelete="SET NULL"))

    # fractions of the frame, 0..1
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float, default=0.0)

    embedding: Mapped[list[float]] = mapped_column(HALFVEC(FACE_DIMS))
    # How old this face looks, in years. Not who they are — how old they look,
    # which is a different question and the only one that separates two sisters
    # photographed at the same age three years apart. A person's birth year is
    # then this subtracted from the date on the photograph, and it should come
    # out the same from every picture of them ever taken.
    apparent_age: Mapped[float | None] = mapped_column(Float)
    # how much detail the face carries, measured at a fixed size. A face out of
    # focus still embeds, and it embeds near every other face out of focus, so
    # without this everyone in the archive who was ever mis-focused assembles
    # into one person made of smears
    sharpness: Mapped[float | None] = mapped_column(Float, index=True)
    # Ten numbers: both eyes, the nose and the corners of the mouth, as fractions
    # of the frame. They are what lets one face be laid over another — without them
    # a run of portraits through the years jumps about, because a box says where a
    # face is and not which way it is turned.
    landmarks: Mapped[list[float] | None] = mapped_column(ARRAY(Float))
    # Where in a recording this face was, in seconds; empty for a still. A
    # recording is a photograph that lasts a while, and the one thing it can say
    # that a photograph cannot is WHEN somebody is in it — so a shelf that opens
    # a twenty-minute christening at the start, when she appears at four
    # minutes, has thrown away the only extra it had.
    at_seconds: Mapped[float | None] = mapped_column(Float)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    found_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())

    cluster: Mapped[FaceCluster | None] = relationship(back_populates="faces")
