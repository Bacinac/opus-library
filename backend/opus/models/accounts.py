"""Who lives here.

The household's roster sits with the catalogue and not with the tool that
fetches things, which is where it started. Three properties say the same thing:
Downloads is the module you can switch off without anybody noticing until
something new fails to arrive; Player is a surface and there will be several of
them; and Library has to be running for anything at all to work. A roster in the
most disposable module was backwards.

It also puts an account beside the richest account of these people the system
has — the hundred and twenty-three faces in `photo_people`, with their birth
dates and their portraits through the years. An account may point at one of
them, and then `filip` who signs in and "Filip Babić" who is in forty thousand
photographs are one person. Point at, not merge: most of those faces are
grandparents and friends who will never sign in, and the account that maintains
the library is nobody's face at all."""

import datetime
import secrets

from opus_auth import ADMIN, GUEST, USER
from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, LargeBinary, String,
                        Text, case, func)
from sqlalchemy.orm import Mapped, mapped_column

from opus.models.base import Base


class User(Base):
    """A person who may sign in — to any of the three modules, since they share
    a door.

    The version rises whenever the secret does and rides along in every session
    cookie, so changing a password ends that person's sessions everywhere at
    once without any module having to be told."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    display: Mapped[str] = mapped_column(String(120), default="")
    secret: Mapped[str] = mapped_column(Text)
    # The three kinds of person an install has, in the order of how much of it
    # is theirs. An admin keeps the place: the roster, the settings, the engines,
    # and every judgement the catalogue records. A user lives here — they read
    # the whole shared library and keep a vault of their own. A guest was handed
    # the address for an evening: the films, the series and the records. One
    # word rather than two flags, so "may change things" and "is one of us"
    # cannot disagree.
    role: Mapped[str] = mapped_column(String(8), default=USER, server_default=USER)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # A name can be removed and reused. Private data belongs to this permanent
    # account identity instead.
    identity: Mapped[str] = mapped_column(
        String(32), unique=True, default=lambda: secrets.token_hex(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # which face in the family library this account belongs to, where it belongs
    # to one. Nullable both ways round on purpose: an account that only ever
    # maintains the place is not a person in a photograph, and almost nobody in
    # the photographs will ever sign in.
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("photo_people.id", ondelete="SET NULL"), nullable=True, unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class Device(Base):
    """A box somebody let in.

    A television is not a person and cannot be asked to be one: its only input
    is four arrow keys, and a password typed with a remote is a password that
    ends up short, shared, and eventually written on the fridge. So a box does
    not sign in — it is **let in**, once, by somebody who already is signed in.
    It shows a code, an admin says yes to that code, and it is holding a
    credential of its own from then on.

    Which is the answer to "how do I stop somebody else's television signing in
    to my address": nothing that has not been said yes to gets in, and every
    box that has is on a list with a name on it that can be taken back.

    The token is kept as a digest and never again in the clear. It is shown once,
    to the module that asked on the box's behalf, and after that this row can
    recognise it but not reproduce it — the same reason a password is not kept
    either.

    A pending row is a code somebody is looking at on a screen right now. It is
    short-lived on purpose: a code that is good for a week is a code that was
    photographed."""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # what a person reads off the television and types in. Unique only among the
    # pending: it is cleared when the box is let in, because it has done its job
    # and a spent code lying about is a spent code somebody tries
    code: Mapped[str | None] = mapped_column(String(16), nullable=True, unique=True)
    # sha256 of what the box holds. Null until somebody says yes
    token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    # sha256 of the claim handed to the module that asked, and only to it. Null
    # once the token is collected
    claim: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # what the module asking called itself, before anybody has named it
    module: Mapped[str] = mapped_column(String(32), default="")
    # what the person who let it in called it. "The one in the living room" is
    # worth more than any fingerprint when the question is which to take back
    name: Mapped[str] = mapped_column(String(64), default="")
    # who said yes, kept because it is the only record of that decision
    let_in_by: Mapped[str] = mapped_column(String(64), default="")
    let_in_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # so a list of boxes can say which one has not been switched on since March
    seen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class Passkey(Base):
    """A key a person's own device holds and unlocks with a fingerprint, a face
    or its PIN, which opens OPUS instead of the password.

    Only the public half is here: what the device signs can be checked with it,
    and nothing kept here could sign anything. It belongs to the shared domain
    rather than to one module, so the key added once opens all three doors."""

    __tablename__ = "passkeys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    credential: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    # a device that counts its signatures must never go backwards: one that does
    # is a copy of the key
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


# How a roster reads to a person looking at it: whoever keeps the place first,
# then the household, then whoever is only passing through. Below the class
# because it names a column, and `value=` because without it every key would be
# read as a condition rather than as something to match.
RANK = case({ADMIN: 0, USER: 1, GUEST: 2}, value=User.role, else_=3)
