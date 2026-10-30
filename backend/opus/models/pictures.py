"""The pictures the library draws, which both halves hand out and neither owns."""

import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from opus.models.base import Base

WANTED = "wanted"
HELD = "held"
# the source answered, and what it answered is that there is no picture there
NONE = "none"
# the source did not answer this time; asked again at retry_at
FAILED = "failed"


class Picture(Base):
    """A picture, by where the catalogue found it.

    `source` is that address — a catalogue's, or a file in the library tree when
    the picture was found there first — and stays what the row is known by.
    `file` is where the original is now: the library's own file when the record
    or the film carries one beside it, otherwise the copy in the store."""

    __tablename__ = "pictures"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(Text, unique=True)
    state: Mapped[str] = mapped_column(String(8), default=WANTED, server_default=WANTED, index=True)
    file: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
