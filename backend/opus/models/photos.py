"""The photo half of the library: photographs and the clips that sit among them,
catalogued where they already are.

Nothing here moves a file. The other two halves name what they import and put it
on a shelf; a photograph was on the shelf before this application existed and
will be there after it. So the row describes a file rather than owning one, and
the only thing this half is allowed to change is its own opinion.

Two tables where the other halves have one, because a photograph and a path are
not the same fact. The same picture is often on the disk twice — once where the
phone put it and once where somebody filed it — and content-addressing makes that
one photograph with two files rather than two photographs that happen to match.

The music half is in `opus.models.music`, the video half in `opus.models.video`."""

import datetime

from sqlalchemy import text as sa_text
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opus.models.base import Base

# what a file row is at this moment. `missing` is a tombstone, not a deletion:
# a disk that was unmounted for an afternoon must not cost the library its
# names, its albums or its faces when it comes back.
FILE_PRESENT = "present"
FILE_MISSING = "missing"
FILE_QUARANTINED = "quarantined"

# where the capture moment came from, kept beside the moment itself, in falling
# order of confidence. A scanned print has no EXIF, a filename says 2003 with no
# more precision than that, and a stripped photograph has only the month of the
# folder it was filed in; a timeline that cannot say which of those it is
# showing invites the reader to trust them equally.
TIME_EXIF = "exif"
TIME_FILENAME = "filename"
# the directory a photograph was filed in. Immich's storage template writes
# <user>/YYYY/MM/, so the tree itself remembers a month for photographs whose
# EXIF was stripped before they ever arrived — hearsay, but hearsay with a date,
# and it survives Immich being switched off in a way its database does not.
TIME_PATH = "path"
# Not a date anybody recorded: the day the household's own faces agree on. A
# recording with eleven known people in it is a fingerprint of one afternoon,
# and the archive knows which afternoon that was. Ranked above the file's own
# modification time and below every other, and that is the whole of its claim:
# an mtime on a tape digitised twenty years later is the day it was COPIED, and
# a christening dated by the people at it beats the day somebody dragged it
# between two disks.
TIME_PEOPLE = "people"
TIME_FILE_MTIME = "mtime"
TIME_NONE = "none"


class Photo(Base):
    """One photograph, addressed by what it contains rather than where it is."""

    __tablename__ = "photos"
    # The timeline pages by (taken_at, id) — the id is the tie-break that stops
    # a burst sharing one second from repeating a row across a page boundary —
    # and this is the composite that makes that one index seek instead of a sort
    # of the whole shelf per page. It supersedes a lone index on taken_at:
    # taken_at leads it, so anything that wanted the single-column index gets
    # this one. Declared here rather than only in a migration, because an index
    # the model does not know about is an index the next autogenerate offers to
    # drop — which is exactly what it offered.
    __table_args__ = (
        Index("ix_photos_timeline", "taken_at", "id"),
        # The list of places asks, once per place, which photograph taken there
        # most recently has a picture made of it. On the name alone that is a
        # sort per place; with the date in the index it is a read of the first
        # row. A hundred and eighty-nine places is a hundred and eighty-nine of
        # them either way.
        Index("ix_photos_place_time", "place", "country",
              sa_text("taken_at DESC NULLS LAST")),
        Index("ix_photos_live_token", "live_token",
              postgresql_where=sa_text("live_token <> ''")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # SHA-1 rather than something stronger on purpose: Immich already holds a
    # SHA-1 for all 45,067 assets, so the catalogue can be seeded from it and
    # the first contribution from a phone already knows the whole library. This
    # is content addressing, not a signature.
    checksum: Mapped[bytes] = mapped_column(LargeBinary(20), unique=True, index=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0)
    kind: Mapped[str] = mapped_column(String(8), default="image")  # image | video
    # who put it here, when it came from somebody's vault rather than from the
    # tree. Empty for everything that was already in the library, which is most
    # of it and always will be.
    offered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    taken_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # the camera's own offset when it recorded one. Without it a capture time is
    # a wall clock with no zone, and converting it is guessing.
    taken_offset: Mapped[str] = mapped_column(String(8), default="")
    taken_source: Mapped[str] = mapped_column(String(10), default=TIME_NONE)

    device_make: Mapped[str] = mapped_column(String(64), default="")
    device_model: Mapped[str] = mapped_column(String(64), default="")
    # the shape the picture is SHOWN in — the file's orientation and the turn
    # below both applied — because every face box is a fraction of that frame
    pixel_w: Mapped[int | None] = mapped_column(Integer)
    pixel_h: Mapped[int | None] = mapped_column(Integer)
    # Clockwise degrees a person turned the picture by, on top of whatever the
    # file says. A phone held upright that recorded itself as lying down writes
    # orientation 1 and sideways pixels, so the file cannot be trusted to know;
    # the original is never rewritten, only its derivatives are drawn turned.
    turn: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Where it was taken. Decimal degrees as the file gives them, and nothing
    # derived from them here: a place NAME is a different fact with a different
    # source, and turning one into the other is somebody else's pass.
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    altitude: Mapped[float | None] = mapped_column(Float)
    # What the place is called. A separate fact from the coordinates and with a
    # different source: a camera without a receiver knows nothing, and the person
    # who was there knows exactly. Which is why the source is recorded — a name
    # somebody typed is never overwritten by one worked out later from numbers.
    place: Mapped[str] = mapped_column(String(120), default="", index=True)
    # ISO-3166 alpha-2. A code rather than a name: the name of a country is a
    # question about language and this is a question about which country
    country: Mapped[str] = mapped_column(String(2), default="", index=True)
    place_source: Mapped[str] = mapped_column(String(16), default="")
    # what the camera was doing. Kept because it is free — one exiftool run
    # already opens every file — and because it is what a person means when they
    # ask which of these was taken with the good lens
    lens: Mapped[str] = mapped_column(String(96), default="")
    focal_mm: Mapped[float | None] = mapped_column(Float)
    aperture: Mapped[float | None] = mapped_column(Float)
    iso: Mapped[int | None] = mapped_column(Integer)
    shutter: Mapped[str] = mapped_column(String(24), default="")
    # the video half of a Live Photo, which is a second file the phone never
    # shows you and which is lost the moment the two are catalogued apart
    live_pair_id: Mapped[int | None] = mapped_column(
        ForeignKey("photos.id", ondelete="SET NULL"), index=True)
    # the identifier Apple writes into both halves; empty once read and absent,
    # null until read
    live_token: Mapped[str | None] = mapped_column(String(64))

    # Twenty-one bytes that draw a blurred impression of the picture — not the
    # picture, the *sense* of it. Small enough to sit in the row and travel with
    # the query, so a grid has something in the right colours to draw before any
    # file has left the disk. The only derivative that belongs in the database,
    # and it belongs there precisely because it is too small to displace
    # anything from the buffer pool.
    thumbhash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    # when the tile and the preview were last written, and by which generation
    # of the recipe — so changing the recipe is a number, not a manual sweep
    derived_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    derived_gen: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # a photograph this recipe cannot read, and which recipe could not read it.
    # Kept on the row and not only in the log: two files out of 41,012 failing
    # is a line written once, at WARNING, into a log that rotates — which is the
    # same as not having been told. On the row it can be counted, shown, and
    # retried on purpose. Carrying the generation is what makes the retry policy
    # fall out for free: a newer decoder is a new generation, and a new
    # generation has never failed on anything.
    derive_failed_gen: Mapped[int] = mapped_column(Integer, default=0)
    derive_error: Mapped[str] = mapped_column(String(200), default="")
    # which generation of the face pass has looked at this photograph. A count
    # of face rows cannot stand in for it: a photograph of a landscape has no
    # faces and that is an answer, not an omission — without the mark it would
    # be re-read on every pass for the rest of the library's life.
    faces_gen: Mapped[int] = mapped_column(Integer, default=0, index=True)

    first_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())

    files: Mapped[list["PhotoFile"]] = relationship(
        back_populates="photo", cascade="all, delete-orphan")


class PhotoFile(Base):
    """A path where a photograph is, and enough of its inode to know whether it
    still is without reading it.

    The four columns after `path` are the whole reason a scan over 45,067 files
    is cheap. A storage template change rewrites every path at once, which looks
    like a mass deletion followed by a mass import and would cost a full rehash
    of the library; the same file under a new name keeps its device and inode, so
    it is recognised with one `lstat` and no bytes read.

    `mtime_ns` is what stops that being dangerous. ext4 reuses inode numbers, and
    this volume also carries a download landing zone with high churn, so a
    recycled inode would otherwise silently re-point a row — with its names and
    its albums — at a different photograph."""

    __tablename__ = "photo_files"
    # Indexed, deliberately not unique. One inode belongs to one file on a live
    # filesystem, but this table also remembers files that are gone, and the
    # number is handed to something else once they are. Uniqueness here would
    # refuse the new photograph rather than the stale row.
    __table_args__ = (Index("ix_photo_files_inode", "dev", "inode"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String, unique=True, index=True)

    dev: Mapped[int] = mapped_column(BigInteger, default=0)
    inode: Mapped[int] = mapped_column(BigInteger, default=0)
    byte_size: Mapped[int] = mapped_column(BigInteger, default=0)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, default=0)

    state: Mapped[str] = mapped_column(String(12), default=FILE_PRESENT, index=True)
    # when the file stopped being found, so a tombstone can be read as "gone
    # since Tuesday" rather than merely "gone"
    missing_since: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    photo: Mapped[Photo] = relationship(back_populates="files")


class PhotoIntegrityEvent(Base):
    """What a pass found that a human should know about.

    A scan that quietly tombstones forty thousand photographs because a mount
    was not ready has done exactly what it was told and exactly the wrong thing.
    Everything the guards refuse to do is written here instead, so the refusal
    is visible rather than merely safe."""

    __tablename__ = "photo_integrity_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    # root_unreadable | mass_missing | moved | missing | returned | quarantined
    kind: Mapped[str] = mapped_column(String(20), index=True)
    path: Mapped[str] = mapped_column(String, default="")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class PhotoOffer(Base):
    """A note that somebody offered this picture, waiting for the pass that
    adopts it to read the note and throw it away.

    Against the checksum and not the path: the catalogue is content-addressed,
    the tree will be reshaped one day, and provenance kept in a filename would
    not survive that."""

    __tablename__ = "photo_offers"

    checksum: Mapped[bytes] = mapped_column(LargeBinary(20), primary_key=True)
    person: Mapped[str] = mapped_column(String(64))
    # which of their own files they offered, so the vault can say "this one is
    # in the library" once the pass has adopted it. The row it will point at
    # does not exist yet when the offer is made.
    vault_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class PhotoShare(Base):
    """Photographs chosen for somebody outside the household, behind a link.

    Only the key's SHA-256 is kept: the link is shown once, when it is made,
    so a copy of the catalogue opens nothing. Withdrawing a share deletes it."""

    __tablename__ = "photo_shares"

    id: Mapped[int] = mapped_column(primary_key=True)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    made_by: Mapped[str] = mapped_column(String(64))
    made_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    photos: Mapped[list["PhotoShareItem"]] = relationship(
        back_populates="share", cascade="all, delete-orphan",
        order_by="PhotoShareItem.position")


class PhotoShareItem(Base):
    """One photograph of a share, in the order it was chosen. The position is
    what the link calls it, so the checksum never leaves the house."""

    __tablename__ = "photo_share_items"

    share_id: Mapped[int] = mapped_column(
        ForeignKey("photo_shares.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), index=True)

    share: Mapped[PhotoShare] = relationship(back_populates="photos")
    photo: Mapped[Photo] = relationship()
