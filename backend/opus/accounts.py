"""Who exists, and how a secret is compared.

The roster lives with the catalogue — see opus.models.accounts for why the tool
that fetches things was the wrong place for it. The other two modules learn a
name from the session cookie and ask here for everything else.

Nothing outside this file compares a secret, and nothing anywhere keeps what was
typed: scrypt over a per-person salt, and the parameters travel inside the stored
value so raising them later does not invalidate everyone at once."""

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager

from opus_auth import ADMIN, ROLES, USER, address_bucket
from sqlalchemy import delete, func, select

from opus import auth
from opus.models import User
from opus.models.accounts import RANK

# ~100 ms and 32 MiB per attempt on this hardware. maxmem has to be said out
# loud: OpenSSL's default ceiling sits exactly at what these parameters need,
# and scrypt fails rather than rounds down.
SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)

# Off the event loop, two at a time, with a short line behind them: every
# module's login ends here, and a flood has to be turned away at the door rather
# than queued in front of everybody else's sign-in.
HASHING = 2
WAITING = 8
_hashing = asyncio.Semaphore(HASHING)
_queued = 0

# Failed attempts over a sliding window, bounded per name from one address and
# per address. Never per name alone: a bound nobody's own address can escape is a
# lock anyone can put on somebody else's sign-in. An attempt still being compared
# counts against the address, so a burst cannot all get in line before the
# first of it has failed.
WINDOW = 15 * 60
FAILS_PER_NAME_FROM_ADDRESS = 10
FAILS_PER_ADDRESS = 30
IN_FLIGHT_PER_ADDRESS = 2
_failures: dict[str, deque[float]] = {}
_in_flight: dict[str, int] = {}


class TooManyAttempts(Exception):
    def __init__(self, retry_after: int):
        super().__init__("too many failed attempts")
        self.retry_after = retry_after


class Busy(Exception):
    retry_after = 2

    def __init__(self):
        super().__init__("too many sign-ins at once")


@asynccontextmanager
async def _hash_turn(refuse: bool):
    global _queued
    if refuse and _queued >= HASHING + WAITING:
        raise Busy()
    _queued += 1
    try:
        async with _hashing:
            yield
    finally:
        _queued -= 1


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${salt.hex()}${dk.hex()}"


async def hashed(secret: str, refuse: bool = False) -> str:
    async with _hash_turn(refuse):
        return await asyncio.to_thread(hash_secret, secret)


async def matches(stored: str, secret: str) -> bool:
    async with _hash_turn(True):
        return await asyncio.to_thread(secret_matches, stored, secret)


def secret_matches(stored: str, secret: str) -> bool:
    try:
        kind, n, salt, want = stored.split("$")
        params = dict(SCRYPT, n=int(n))
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    dk = hashlib.scrypt(secret.encode(), salt=bytes.fromhex(salt), dklen=32, **params)
    return hmac.compare_digest(dk.hex(), want)


# A vault key is derived from its owner's password, so the strength of the one
# is the strength of the other — a four-letter password is a four-letter vault,
# whatever the six hundred thousand rounds on top of it. Eight is the floor, not
# a recommendation, and it is checked here because here is where a password is
# set from all three modules.
SHORTEST = 8


def too_short(secret: str) -> bool:
    return len(secret or "") < SHORTEST


def standing(role: str | None) -> str:
    """A word this roster knows, or the middle one.

    Anything unrecognised becomes an ordinary member of the household rather
    than an admin: a typo must not be able to hand somebody the place, and it
    must not lock out somebody who belongs here either."""
    role = (role or "").strip().lower()
    return role if role in ROLES else USER


def tidy_name(name: str) -> str:
    """One spelling per person. A name is how a session says who it is for, so
    it cannot be case-sensitive without two people being able to hold what looks
    to everyone else like one account."""
    return " ".join(name.split()).lower()


async def everyone(session) -> list[User]:
    rows = await session.execute(select(User).order_by(RANK, User.name))
    return list(rows.scalars())


async def by_name(session, name: str) -> User | None:
    rows = await session.execute(select(User).where(User.name == tidy_name(name)))
    return rows.scalar_one_or_none()


async def _next_session_version(session) -> int:
    """A cookie generation is never reused, even after its account is gone."""
    return int(await session.scalar(select(func.nextval("users_session_version_seq"))))


def _recent(key: str, now: float) -> deque[float]:
    kept = _failures.setdefault(key, deque())
    while kept and kept[0] <= now - WINDOW:
        kept.popleft()
    return kept


def _sweep(now: float) -> None:
    for key in [k for k, kept in _failures.items() if not kept or kept[-1] <= now - WINDOW]:
        del _failures[key]


def _admit(counted: tuple, caller: str, now: float) -> None:
    for key, most in counted:
        kept = _recent(key, now)
        if len(kept) + _in_flight.get(key, 0) >= most:
            raise TooManyAttempts(int(kept[0] + WINDOW - now) + 1 if kept else Busy.retry_after)
    if _in_flight.get(caller, 0) >= IN_FLIGHT_PER_ADDRESS:
        raise TooManyAttempts(Busy.retry_after)


async def check(session, name: str, secret: str, address: str) -> User | None:
    """The person this credential proves, or None. Raises TooManyAttempts
    without comparing anything once a bound has been reached inside the window,
    and Busy when the line for a comparison is already full."""
    now = time.monotonic()
    if len(_failures) > 10_000:
        _sweep(now)
    bucket = address_bucket(address)
    caller = f"address:{bucket}"
    counted = ((f"name:{tidy_name(name)}@{bucket}", FAILS_PER_NAME_FROM_ADDRESS),
               (caller, FAILS_PER_ADDRESS))
    _admit(counted, caller, now)
    for key, _ in counted:
        _in_flight[key] = _in_flight.get(key, 0) + 1
    try:
        person = await by_name(session, name)
        if person is None or person.disabled:
            # the same cost either way, so a stranger cannot learn which names
            # exist by how long the answer takes
            await hashed(secret, refuse=True)
            person = None
        elif not await matches(person.secret, secret):
            person = None
    finally:
        for key, _ in counted:
            if _in_flight[key] == 1:
                del _in_flight[key]
            else:
                _in_flight[key] -= 1
    if person is None:
        for key, _ in counted:
            _recent(key, now).append(now)
    return person


async def add(session, name: str, secret: str, display: str = "",
              role: str = USER, person_id: int | None = None) -> User:
    person = User(name=tidy_name(name), display=display.strip(),
                  secret=await hashed(secret), role=standing(role),
                  version=await _next_session_version(session),
                  person_id=person_id)
    session.add(person)
    await session.commit()
    auth.forget_roster()
    return person


async def amend(session, person: User, *, display: str | None = None,
                secret: str | None = None, role: str | None = None,
                disabled: bool | None = None,
                person_id: int | None = None, unlink: bool = False) -> User:
    if display is not None:
        person.display = display.strip()
    if role is not None:
        person.role = standing(role)
    ends_sessions = (disabled is True and not person.disabled) or bool(secret)
    if disabled is not None:
        person.disabled = disabled
    if unlink:
        person.person_id = None
    elif person_id is not None:
        person.person_id = person_id
    if secret:
        person.secret = await hashed(secret)
    if ends_sessions:
        # This is a fresh global generation, rather than an increment a new
        # account with a recycled name could inherit.
        person.version = await _next_session_version(session)
    await session.commit()
    auth.forget_roster()
    return person


async def remove(session, person: User):
    await session.execute(delete(User).where(User.id == person.id))
    await session.commit()
    auth.forget_roster()


async def admins(session) -> int:
    return await session.scalar(
        select(func.count()).select_from(User)
        .where(User.role == ADMIN, User.disabled.is_(False))
    ) or 0


async def lock_roster(session) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext("opus:accounts"))))


def shown(person: User) -> dict:
    return {
        "name": person.name,
        "display": person.display or person.name,
        "role": person.role,
        "disabled": person.disabled,
        # not a secret, and the guards next door cannot tell a current session
        # from one issued before a password change without it
        "version": person.version,
        "identity": person.identity,
        # which face in the family library this account is, where it is one
        "person_id": person.person_id,
    }
