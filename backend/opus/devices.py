"""Letting a box in, and taking it back.

A person signs in. A box is let in — the difference is not pedantry, it is the
only shape that works for a screen whose entire input is four arrow keys. A
password typed with a remote is a password that ends up short, shared, and
eventually the answer to "how did somebody else's television get into my
address".

So: the box shows a code, somebody who is already signed in says yes to that
code, and the box holds a credential of its own from then on. Nothing that has
not been said yes to is in, every box that is in has a name, and any of them can
be taken back without touching the others.

The token is generated once, handed once to the module that asked, and kept here
only as a digest — the same treatment a password gets, for the same reason. It is
handed only to whoever holds the claim that came back with the code: the code is
read aloud across a room, and the row's number is one more than the last one, so
neither of them can be what collects a credential."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from opus.models import Device

# No O, I, 0 or 1. Somebody is reading this off a television across a room and
# typing it on a phone, and the pairs that letter-shape confusion turns into
# each other are the ones that make a code feel broken rather than mistyped.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
GROUPS, PER_GROUP = 2, 4

# A code is good for as long as somebody is walking to the other room with it. A
# code good for a week is a code that has been photographed.
GOOD_FOR = timedelta(minutes=15)


def make_code() -> str:
    parts = ["".join(secrets.choice(ALPHABET) for _ in range(PER_GROUP))
             for _ in range(GROUPS)]
    return "-".join(parts)


def tidy_code(given: str) -> str:
    """What somebody typed, as this list spells it. Case and the dash are
    presentation: refusing `k7f2 9qx3` because of them would be refusing a
    correct answer."""
    kept = [c for c in (given or "").upper() if c in ALPHABET]
    return "-".join("".join(kept[i:i + PER_GROUP])
                    for i in range(0, len(kept), PER_GROUP))


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _stale() -> datetime:
    return datetime.now(timezone.utc) - GOOD_FOR


async def ask(session, module: str) -> tuple[Device, str]:
    """A box asking to be let in, and the claim only the asker holds. Nothing is
    granted here — this is the code that goes on the screen, and it is worth
    nothing until somebody says yes."""
    # only codes nobody ever said yes to. A box that was let in and has not
    # come to collect yet is switched off, not abandoned
    await session.execute(
        delete(Device).where(Device.let_in_at.is_(None), Device.created_at < _stale()))
    claim = secrets.token_urlsafe(32)
    device = Device(code=make_code(), module=module[:32], claim=digest(claim))
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return device, claim


async def waiting(session, code: str) -> Device | None:
    """The box behind a code somebody has just typed, if the code is still worth
    anything. Expiry is checked here rather than swept by a timer: a code that
    ran out a second ago must be refused whether or not anything has tidied up
    since."""
    rows = await session.execute(
        select(Device).where(Device.code == tidy_code(code),
                             Device.let_in_at.is_(None),
                             Device.created_at >= _stale()))
    return rows.scalar_one_or_none()


async def let_in(session, device: Device, name: str, by: str):
    """Say yes.

    No token is made here. The person saying yes is standing at a different
    screen from the box, and a secret minted at this moment would have to be
    kept somewhere until the box came to collect it — in memory, where a restart
    loses it, or in the clear on this row, where it is a password written down.
    It is minted when the box collects instead, which is the same moment from
    everybody's point of view and needs neither."""
    # the code has done its job, and a spent code lying about is a spent code
    # somebody eventually tries
    device.code = None
    device.name = (name or "").strip()[:64] or device.module or "device"
    device.let_in_by = by[:64]
    device.let_in_at = datetime.now(timezone.utc)
    await session.commit()


async def collect(session, device_id: int, claim: str) -> dict | None:
    """Has anybody said yes, and the credential if this is the moment it is
    collected — made then and never obtainable again.

    None for a claim that does not match, a code that ran out, or a credential
    already collected: all three mean the box asks for a new code, which is the
    only honest thing to do about a secret nobody can look up — including us.
    The mint is one conditional statement, so two callers holding the same claim
    cannot both walk away with a token."""
    token = secrets.token_urlsafe(32)
    minted = (await session.execute(
        update(Device)
        .where(Device.id == device_id, Device.claim == digest(claim),
               Device.let_in_at.is_not(None), Device.token.is_(None))
        .values(token=digest(token), claim=None)
        .returning(Device.name))).scalar_one_or_none()
    await session.commit()
    if minted is not None:
        return {"in": True, "token": token, "name": minted}
    waiting = (await session.execute(
        select(Device.id).where(Device.id == device_id, Device.claim == digest(claim),
                                Device.let_in_at.is_(None),
                                Device.created_at >= _stale()))).scalar_one_or_none()
    return None if waiting is None else {"in": False, "token": None, "name": ""}


async def claimed(session, device_id: int) -> Device | None:
    return await session.get(Device, device_id)


async def holding(session, token: str) -> Device | None:
    """The box holding this token, if any box does. A token nobody holds is not
    an error — it is a device that was taken back, and the caller's job is to
    turn it away rather than to explain."""
    if not token:
        return None
    rows = await session.execute(select(Device).where(Device.token == digest(token)))
    return rows.scalar_one_or_none()


async def seen(session, device: Device):
    """Noted at most once a minute. This is written on requests that are
    otherwise pure reads, and a write per thumbnail would be a write per
    thumbnail."""
    now = datetime.now(timezone.utc)
    if device.seen_at and (now - device.seen_at) < timedelta(minutes=1):
        return
    device.seen_at = now
    await session.commit()


async def everyone(session) -> list[Device]:
    """Every box that is in, and every code still on a screen. Whatever is
    waiting for somebody comes first, because that is the row a person opening
    this list has walked over to act on."""
    rows = await session.execute(
        select(Device)
        .where((Device.let_in_at.is_not(None)) | (Device.created_at >= _stale()))
        .order_by(Device.let_in_at.is_(None).desc(), Device.let_in_at.desc()))
    return list(rows.scalars())


async def rename(session, device: Device, name: str):
    device.name = name.strip()[:64]
    await session.commit()


async def take_back(session, device: Device):
    await session.execute(delete(Device).where(Device.id == device.id))
    await session.commit()


def shown(device: Device) -> dict:
    # Never the code. Saying yes is an act only while the code has to be read
    # off the television itself; a list that carried it would let somebody
    # else's box in on a distracted click.
    return {
        "id": device.id,
        "name": device.name,
        "module": device.module,
        # said yes to but not yet holding anything: a television switched off
        # between the yes and the collecting
        "collected": device.token is not None,
        "let_in_by": device.let_in_by,
        "let_in_at": device.let_in_at.isoformat() if device.let_in_at else None,
        "seen_at": device.seen_at.isoformat() if device.seen_at else None,
    }
