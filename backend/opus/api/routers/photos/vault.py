"""The private half: one person's own store, which this server cannot read.

Every route here answers for whoever is signed in and for nobody else. There is
no route that takes a person's name — not for an owner, not for the household,
not for maintenance. An administrator can delete a vault (it is on their disk)
and can see how large it is; they cannot open one. That asymmetry is the feature.

A service token opens the other routers, because Player is a module acting for
the household. It opens nothing here: a token is not a person, and everything in
a vault belongs to one."""

import asyncio
import base64
import binascii
import secrets

import opus_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from opus_core.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opus import auth
from opus.db import get_session
from opus.models import User, VaultFile, VaultKey
from opus.photos import room
from opus.photos.library import offers
from opus.photos.vault import opening
from opus.photos.vault import store
from opus.settings_store import current_runtime

router = APIRouter()

# how many rows a client takes at a time while it builds its own index. The blob
# on each is a few hundred bytes, so this is a page of a few hundred kilobytes —
# one round trip per thousand pictures.
PAGE = 1000

# one writer per file: two pieces sent for the same offset must not both land
_writing: set[str] = set()


class Enrolment(BaseModel):
    salt: str
    rounds: int
    wrapped: str
    recovery_salt: str
    recovery: str


class Rewrap(BaseModel):
    salt: str
    rounds: int
    wrapped: str


class Reservation(BaseModel):
    mark: str
    bytes: int
    chunk: int
    keyed: str
    meta: str


def _raw(value: str, field: str, limit: int = 4096) -> bytes:
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, f"{field} is not base64")
    if not data or len(data) > limit:
        raise HTTPException(400, f"{field} is not a plausible length")
    return data


def _b64(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode() if data is not None else None


def _reserved(row: VaultFile, known: bool, config) -> dict:
    """Return the cryptographic material originally bound to an upload."""
    return {"id": row.id, "at": row.at, "bytes": row.size, "chunk": row.chunk,
            "keyed": _b64(row.keyed), "meta": _b64(row.meta), "known": known,
            "thumb": row.thumb is not None and store.thumb_at(config, row.person, row.id).is_file()}


async def whose(request: Request, session: AsyncSession = Depends(get_session)) -> str:
    """Whose vault this call is about — the signed-in person, always.

    An install with no door at all (first run, or a standalone dev box) has
    nobody to own a vault, and saying so plainly beats inventing a name for
    files that would then belong to no one."""
    person = auth.whoami(await auth.roster(session),
                         request.cookies.get(opus_auth.SESSION_COOKIE))
    if person is None:
        raise HTTPException(403, "a vault belongs to a person, and none is signed in")
    owner = await session.scalar(select(User.identity).where(User.name == person))
    if owner is None:
        raise HTTPException(403, "a vault belongs to a person, and none is signed in")
    return owner


async def _file(session, person: str, file_id: str) -> VaultFile:
    row = await session.scalar(
        select(VaultFile).where(VaultFile.id == file_id, VaultFile.person == person))
    if row is None:
        # the same answer for somebody else's file and for one that does not
        # exist: which of the two it is, is not the asker's business
        raise HTTPException(404, "no such file")
    return row


@router.get("/vault/keys")
async def keys(person: str = Depends(whose), session: AsyncSession = Depends(get_session)):
    """What a device needs to unlock this vault, and nothing that could unlock
    it. Both wraps travel: the second one is how a person who has changed their
    password next door gets back in."""
    row = await session.get(VaultKey, person)
    if row is None:
        return {"enrolled": False}
    return {
        "enrolled": True,
        "salt": _b64(row.salt),
        "rounds": row.rounds,
        "wrapped": _b64(row.wrapped),
        "recovery_salt": _b64(row.recovery_salt),
        "recovery": _b64(row.recovery),
        "chunk": store.CHUNK,
    }


@router.post("/vault/keys")
async def enrol(body: Enrolment, person: str = Depends(whose),
                session: AsyncSession = Depends(get_session)):
    """Making a vault. Once, and never again over the top of one that holds
    files: a second key does not re-encrypt the first key's pictures, it
    abandons them."""
    if await session.get(VaultKey, person):
        raise HTTPException(409, "this vault already has a key")
    session.add(VaultKey(
        person=person,
        salt=_raw(body.salt, "salt", 64), rounds=body.rounds,
        wrapped=_raw(body.wrapped, "wrapped"),
        recovery_salt=_raw(body.recovery_salt, "recovery_salt", 64),
        recovery=_raw(body.recovery, "recovery"),
    ))
    await session.commit()
    return {"enrolled": True, "chunk": store.CHUNK}


@router.patch("/vault/keys")
async def rewrap(body: Rewrap, person: str = Depends(whose),
                 session: AsyncSession = Depends(get_session)):
    """The same key, wrapped under a new password.

    Done by a device that already holds the key, which is the only thing that
    can: the server has never seen it and cannot check the work. It does not
    need to — whoever is signed in can already read and delete everything here,
    so a bad rewrap is a way to lock yourself out, not a way in. The recovery
    wrap is deliberately left alone, because it is what a bad rewrap is
    survived with."""
    row = await session.get(VaultKey, person)
    if row is None:
        raise HTTPException(404, "there is no vault to rewrap")
    row.salt = _raw(body.salt, "salt", 64)
    row.rounds = body.rounds
    row.wrapped = _raw(body.wrapped, "wrapped")
    await session.commit()
    return {"rewrapped": True}


@router.get("/vault")
async def index(after: str = "", person: str = Depends(whose),
                session: AsyncSession = Depends(get_session)):
    """The whole index, a page at a time, so a client can hold its own copy.

    Ordered by id rather than by anything about the pictures, because the server
    knows nothing about the pictures. The client sorts by date once it has
    decrypted what it fetched."""
    query = select(VaultFile).where(VaultFile.person == person)
    if after:
        query = query.where(VaultFile.id > after)
    rows = (await session.scalars(query.order_by(VaultFile.id).limit(PAGE))).all()
    return {
        "files": [{
            "id": row.id,
            "mark": _b64(row.mark),
            "bytes": row.size,
            "at": row.at,
            "chunk": row.chunk,
            "keyed": _b64(row.keyed),
            "meta": _b64(row.meta),
            "thumb": row.thumb is not None,
            "photo_id": row.photo_id,
        } for row in rows],
        "more": len(rows) == PAGE,
    }


@router.get("/vault/stat")
async def stat(person: str = Depends(whose), session: AsyncSession = Depends(get_session)):
    config = await current_runtime()
    total, done, held = (await session.execute(
        select(func.count(), func.count().filter(VaultFile.at >= VaultFile.size),
               func.coalesce(func.sum(VaultFile.at), 0))
        .where(VaultFile.person == person))).one()
    return {"files": total, "complete": done, "bytes": int(held),
            "chunk": store.CHUNK, "spare": max(0, store.spare(config))}


@router.post("/vault")
async def reserve(body: Reservation, person: str = Depends(whose),
                  session: AsyncSession = Depends(get_session)):
    """Announce a file before sending it, and find out how much of it is
    already here.

    The same call covers three cases a phone cannot tell apart on its own: a new
    picture, one it already sent (the mark collides, nothing to do), and one it
    began sending before the connection dropped. All three answer with an id and
    an offset, and the phone sends from that offset."""
    mark = _raw(body.mark, "mark", 64)
    if body.bytes <= 0:
        raise HTTPException(400, "a file with no bytes is not a file")
    if body.bytes > room.LARGEST:
        raise HTTPException(413, "a file this large is not taken")
    if not 0 < body.chunk <= store.CHUNK:
        raise HTTPException(400, f"a piece is at most {store.CHUNK} bytes")
    config = await current_runtime()
    if not store.room_for(config, body.bytes):
        # said before a byte is accepted rather than discovered halfway up a
        # video, and said loudly: a phone that is told "no room" stops and can
        # be believed, where one that fails mid-write leaves a part-file behind
        # and tries again tomorrow
        raise HTTPException(507, "there is not enough room left for this file")
    known = await session.scalar(select(VaultFile).where(
        VaultFile.person == person, VaultFile.mark == mark))
    if known is not None:
        # the disk is the truth; the row is only what we believed last time.
        # More on disk than was announced is not the file, and it starts over
        on_disk = store.held(config, person, known.id)
        if on_disk > known.size:
            store.discard(config, person, known.id)
            on_disk = 0
        if on_disk != known.at:
            known.at = on_disk
            await session.commit()
        return _reserved(known, True, config)
    row = VaultFile(
        id=secrets.token_hex(16), person=person, mark=mark, size=body.bytes,
        at=0, chunk=body.chunk, keyed=_raw(body.keyed, "keyed"),
        meta=_raw(body.meta, "meta", 16384),
    )
    session.add(row)
    await session.commit()
    return _reserved(row, False, config)


@router.put("/vault/{file_id}")
async def send(file_id: str, request: Request, at: int = 0,
               person: str = Depends(whose),
               session: AsyncSession = Depends(get_session)):
    """The next piece. Refused unless it starts exactly where the file ends —
    a hole would not be noticed until the day someone needed the picture."""
    row = await _file(session, person, file_id)
    config = await current_runtime()
    data = await _body(request, row.chunk + opening.TAG)
    if not data:
        raise HTTPException(400, "nothing to add")
    if at + len(data) > row.size:
        raise HTTPException(400, "that is more than the file was announced to be")
    if not store.room_for(config, len(data)):
        raise HTTPException(507, "there is not enough room left")
    if file_id in _writing:
        raise HTTPException(409, "a piece of this file is already being written")
    _writing.add(file_id)
    try:
        size = await asyncio.to_thread(store.append, config, person, file_id, at, data)
    except ValueError as gap:
        raise HTTPException(409, str(gap))
    finally:
        _writing.discard(file_id)
    row.at = size
    await session.commit()
    return {"at": size, "bytes": row.size, "complete": size >= row.size}


async def _body(request: Request, most: int) -> bytes:
    """The request body, refused as soon as it passes what one piece can be
    rather than after it has been held whole."""
    got = bytearray()
    async for piece in request.stream():
        got += piece
        if len(got) > most:
            raise HTTPException(413, f"a piece is at most {most} bytes")
    return bytes(got)


@router.put("/vault/{file_id}/thumb")
async def send_thumb(file_id: str, request: Request, person: str = Depends(whose),
                     session: AsyncSession = Depends(get_session)):
    """The small encrypted picture the grid is drawn from. Made on the device,
    because here there is nothing to make it from."""
    row = await _file(session, person, file_id)
    data = await _body(request, 2 * 1024 * 1024)
    if not data:
        raise HTTPException(400, "not a plausible thumbnail")
    config = await current_runtime()
    await asyncio.to_thread(store.put_thumb, config, person, file_id, data)
    row.thumb = b"\x01"
    await session.commit()
    return {"thumb": True}


@router.get("/vault/{file_id}/thumb")
async def read_thumb(file_id: str, person: str = Depends(whose),
                     session: AsyncSession = Depends(get_session)):
    await _file(session, person, file_id)
    config = await current_runtime()
    path = store.thumb_at(config, person, file_id)
    if not path.exists():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(path, media_type="application/octet-stream")


@router.get("/vault/{file_id}/bytes")
async def read_bytes(file_id: str, person: str = Depends(whose),
                     session: AsyncSession = Depends(get_session)):
    row = await _file(session, person, file_id)
    if row.at < row.size:
        raise HTTPException(409, "this file never finished arriving")
    config = await current_runtime()
    return FileResponse(store.where(config, person, file_id),
                        media_type="application/octet-stream")


@router.delete("/vault/{file_id}")
async def forget(file_id: str, person: str = Depends(whose),
                 session: AsyncSession = Depends(get_session)):
    await _file(session, person, file_id)
    config = await current_runtime()
    store.forget(config, person, file_id)
    await session.execute(delete(VaultFile).where(
        VaultFile.id == file_id, VaultFile.person == person))
    await session.commit()
    return {"removed": file_id}


class Offering(BaseModel):
    key: str
    base: str
    name: str = ""


@router.post("/vault/{file_id}/offer")
async def offer(file_id: str, body: Offering, person: str = Depends(whose),
                session: AsyncSession = Depends(get_session)):
    """Give the household one picture out of this vault.

    The device sends the key to that one file, not the file. It can, because the
    picture is on its way to being seen by everyone in the house anyway — and
    every file was sealed under its own random key, so what is handed over opens
    this photograph and nothing else. The vault key stays on the device.

    What that buys is the second journey: a video of two gigabytes is already
    here, and decrypting our own copy costs no upload at all.

    The vault keeps its copy. Offering is a copy and never a move, or a picture
    deleted from the shared library a year from now would take somebody's only
    backup with it."""
    row = await _file(session, person, file_id)
    if row.at < row.size:
        raise HTTPException(409, "this file never finished arriving")
    config = await current_runtime()

    waiting = offers.waiting_room(config)
    if room.spare(waiting) < row.size:
        raise HTTPException(507, "there is not enough room left in the library")
    # written under a name the pass will not adopt, because what it is is not
    # known until it has been read to the end
    working = waiting / f".{secrets.token_hex(8)}.opening"
    try:
        try:
            checksum, size = await asyncio.to_thread(
                opening.open_into, store.where(config, person, file_id), working,
                _raw(body.key, "key", 32), _raw(body.base, "base", 8), row.chunk, row.size)
        except opening.WrongKey as wrong:
            raise HTTPException(400, str(wrong))
        except opening.NotWhole as cut:
            raise HTTPException(409, str(cut))
        said = await offers.already(session, checksum, person, file_id)
        if said is not None:
            return said
        landed = await offers.give(session, working, config, body.name, checksum,
                                   person, file_id)
    finally:
        working.unlink(missing_ok=True)

    if landed is None:
        return {"known": True, "pending": True}
    return {"known": False, "at": str(landed), "bytes": size}


@router.head("/vault/{file_id}")
async def peek(file_id: str, person: str = Depends(whose),
               session: AsyncSession = Depends(get_session)):
    row = await _file(session, person, file_id)
    return Response(status_code=200, headers={
        "X-Vault-At": str(row.at), "X-Vault-Bytes": str(row.size)})
