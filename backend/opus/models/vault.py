"""The private half of the photographs: what a phone puts here for one person.

Nothing in this file is readable by the server, and that is the whole design.
The bytes arrive already encrypted, the thumbnail arrives already encrypted, and
everything anyone would want to know about a picture — what it is called, when
it was taken, where, on what — arrives inside one encrypted blob. What is left
in the clear is what a store cannot function without: whose it is, how large it
is, and when it was received.

So there is no server-side timeline over a vault, no faces, no search. A client
fetches the index once and keeps it; the pictures are read on the device that
holds the key. That is not a limitation that was accepted reluctantly — it is
what makes "only she can see it" a fact about the system rather than a promise
about our conduct.

The owner is an opaque account identifier, not a name: names can be reused
after an account is removed, while a vault must remain with its original owner."""

from datetime import datetime

from sqlalchemy import (BigInteger, DateTime, ForeignKey, Index, Integer,
                        LargeBinary, String, func)
from sqlalchemy.orm import Mapped, mapped_column

from opus.models.base import Base


class VaultKey(Base):
    """One person's key to their own vault, kept only in forms the server
    cannot open.

    The vault key itself is random and never leaves the device. It is stored
    twice, wrapped under two different keys derived from two different things
    the person knows: their password, and a recovery code written down once when
    the vault was made. Two wraps because one is not enough — a password that
    changes next door would otherwise take every photograph with it, and a
    forgotten password with no second door is a deleted library.

    There is no third wrap held by an administrator. That is the point of it."""

    __tablename__ = "vault_keys"

    person: Mapped[str] = mapped_column(String(64), primary_key=True)
    # PBKDF2-SHA256, the rounds stored beside the salt so raising them later
    # does not lock out a device that has not opened in a while
    salt: Mapped[bytes] = mapped_column(LargeBinary(32))
    rounds: Mapped[int] = mapped_column(Integer)
    wrapped: Mapped[bytes] = mapped_column(LargeBinary)
    recovery_salt: Mapped[bytes] = mapped_column(LargeBinary(32))
    recovery: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class VaultFile(Base):
    """One file a phone put away. The row is the index; the bytes are on disk.

    `mark` is how the same photograph is recognised on a second phone or a
    second run without the server learning anything: the client sends
    HMAC(vault key, digest of the plaintext), so it collides for identical files
    within one person's vault and is meaningless outside it. A plain digest
    would have let anyone holding the database test whether someone owns a
    particular known image.

    `at` is how far the upload got. A phone loses its connection halfway up a
    forty-megabyte video and has to be able to carry on rather than start
    again."""

    __tablename__ = "vault_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    person: Mapped[str] = mapped_column(String(64))
    mark: Mapped[bytes] = mapped_column(LargeBinary(32))
    size: Mapped[int] = mapped_column("bytes", BigInteger)
    at: Mapped[int] = mapped_column(BigInteger, default=0)
    chunk: Mapped[int] = mapped_column(Integer)
    keyed: Mapped[bytes] = mapped_column(LargeBinary)
    meta: Mapped[bytes] = mapped_column(LargeBinary)
    thumb: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # what it became in the family library, if its owner offered it there. The
    # vault keeps its own copy either way: a photograph deleted from the shared
    # library must never take somebody's backup with it.
    photo_id: Mapped[int | None] = mapped_column(
        ForeignKey("photos.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # the same file twice is one row, and only within one person's vault
        Index("ix_vault_files_person_mark", "person", "mark", unique=True),
        # the index a client pages through, newest first
        Index("ix_vault_files_person_id", "person", "id"),
    )
