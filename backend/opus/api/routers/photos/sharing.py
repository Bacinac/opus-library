"""Photographs handed to somebody outside the household by a link.

A share is a closed list: the photographs chosen when it was made, and nothing
the library gains afterwards. A picture reaches a stranger only because the
person who keeps the library picked it.

What the link opens is the picture and nothing about it: no name, no face, no
place, no date. The preview is the derivative, which never carried the
original's metadata. The original is handed over as a copy with every tag
taken off but the colour profile and which way up it stands, and the copy is
read back before it leaves: a location that survived the strip is a refusal,
not a download."""

import asyncio
import datetime
import hashlib
import json
import re
import secrets
import subprocess
from pathlib import Path

import opus_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from opus_core.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from opus import auth
from opus.db import get_session
from opus.models import FILE_PRESENT, Photo, PhotoShare, PhotoShareItem
from opus.photos.library import derive
from opus.settings_store import current_runtime

router = APIRouter(prefix="/shares")
public = APIRouter(prefix="/shared")

LONGEST_DAYS = 365
MOST_PHOTOS = 500
# Not the shelf's year of immutable caching: a withdrawn link must stop
# showing the pictures, and nothing on the way may keep a copy of them.
KEPT = {"Cache-Control": "private, max-age=3600"}

# EXIF orientation as (mirrored, clockwise degrees), the way exiftool names them
ORIENTATIONS = {1: (False, 0), 2: (True, 0), 3: (False, 180), 4: (True, 180),
                5: (True, 270), 6: (False, 90), 7: (True, 90), 8: (False, 270)}
ROTATIONS = {0: "Horizontal (Normal)", 90: "Rotate 90 CW", 180: "Rotate 180", 270: "Rotate 270 CW"}
LOCATED = re.compile(r"gps|location|latitude|longitude", re.IGNORECASE)


def digest(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


class Made(BaseModel):
    photos: list[str] = Field(min_length=1, max_length=MOST_PHOTOS)
    days: int = Field(ge=1, le=LONGEST_DAYS)


@router.post("")
async def make(body: Made, request: Request, session=Depends(get_session)):
    """The link is shown here and never again: the catalogue keeps only its
    SHA-256, so a copy of the database opens nothing."""
    try:
        wanted = list(dict.fromkeys(bytes.fromhex(c) for c in body.photos))
    except ValueError:
        raise HTTPException(400, "not a checksum")
    found = {p.checksum: p for p in (await session.execute(
        select(Photo).where(Photo.checksum.in_(wanted)))).scalars()}
    if len(found) != len(wanted):
        raise HTTPException(404, "a chosen photograph is no longer in the library")
    if any(p.kind != "image" for p in found.values()):
        raise HTTPException(400, "a recording is not shared by link")

    key = secrets.token_urlsafe(32)
    now = datetime.datetime.now(datetime.UTC)
    share = PhotoShare(
        key_hash=digest(key),
        made_by=auth.whoami(await auth.roster(session),
                            request.cookies.get(opus_auth.SESSION_COOKIE)) or "",
        made_at=now,
        expires_at=now + datetime.timedelta(days=body.days),
        photos=[PhotoShareItem(position=n, photo_id=found[c].id) for n, c in enumerate(wanted)])
    session.add(share)
    await session.commit()
    return {"id": share.id, "key": key, "expires_at": share.expires_at.isoformat(),
            "photos": len(wanted)}


@router.get("")
async def shares(session=Depends(get_session)):
    counted = (select(PhotoShareItem.share_id, func.count().label("n"),
                      func.min(PhotoShareItem.position).label("first"))
               .group_by(PhotoShareItem.share_id).subquery())
    rows = (await session.execute(
        select(PhotoShare, counted.c.n, Photo.checksum, Photo.turn)
        .outerjoin(counted, counted.c.share_id == PhotoShare.id)
        .outerjoin(PhotoShareItem, (PhotoShareItem.share_id == PhotoShare.id)
                   & (PhotoShareItem.position == counted.c.first))
        .outerjoin(Photo, Photo.id == PhotoShareItem.photo_id)
        .order_by(PhotoShare.made_at.desc()))).all()
    return [{"id": s.id, "made_by": s.made_by, "made_at": s.made_at.isoformat(),
             "expires_at": s.expires_at.isoformat(), "photos": n or 0,
             "cover": {"id": checksum.hex(), "turn": turn} if checksum else None}
            for s, n, checksum, turn in rows]


@router.delete("/{share_id}")
async def revoke(share_id: int, session=Depends(get_session)):
    share = await session.get(PhotoShare, share_id)
    if share is None:
        raise HTTPException(404, "no such share")
    await session.delete(share)
    await session.commit()
    return {"revoked": share_id}


async def _open(session, key: str) -> PhotoShare:
    """An expired link and a revoked one say the same thing to whoever holds it."""
    share = (await session.execute(
        select(PhotoShare).where(PhotoShare.key_hash == digest(key))
        .options(selectinload(PhotoShare.photos).selectinload(PhotoShareItem.photo)
                 .selectinload(Photo.files)))).scalar_one_or_none()
    if share is None or share.expires_at <= datetime.datetime.now(datetime.UTC):
        raise HTTPException(404, "this link has expired or was withdrawn")
    return share


def _item(share: PhotoShare, n: int) -> Photo:
    item = next((i for i in share.photos if i.position == n), None)
    if item is None:
        raise HTTPException(404, "no such photograph in this share")
    return item.photo


@public.get("/{key}")
async def shared(key: str, session=Depends(get_session)):
    share = await _open(session, key)
    return {"expires_at": share.expires_at.isoformat(),
            "photos": [{"id": str(i.position), "kind": "image", "taken_at": None, "dated": "",
                        "w": i.photo.pixel_w, "h": i.photo.pixel_h,
                        "hash": i.photo.thumbhash.hex() if i.photo.thumbhash else None,
                        "ready": i.photo.derived_gen == derive.GENERATION,
                        "undecodable": i.photo.derive_failed_gen == derive.GENERATION,
                        "turn": i.photo.turn}
                       for i in share.photos]}


async def _derivative(session, key: str, n: int, which: int):
    photo = _item(await _open(session, key), n)
    path = derive.paths_for(derive.store_root(await current_runtime()), photo.checksum)[which]
    if not path.exists():
        raise HTTPException(404, "not derived yet")
    return FileResponse(path, media_type="image/jpeg" if which == 0 else "image/avif", headers=KEPT)


@public.get("/{key}/{n}/tile")
async def shared_tile(key: str, n: int, session=Depends(get_session)):
    return await _derivative(session, key, n, 0)


@public.get("/{key}/{n}/preview")
async def shared_preview(key: str, n: int, session=Depends(get_session)):
    return await _derivative(session, key, n, 1)


def clean_copy(path: Path, turn: int) -> bytes:
    """The original with nothing about it left but its colours and which way up
    it stands. A turn given by hand goes into the copy's orientation, since the
    original was never rewritten and would otherwise arrive the wrong way up."""
    args = ["exiftool", "-q", "-q", "-all=", "-tagsfromfile", "@", "-icc_profile"]
    if turn:
        said = subprocess.run(["exiftool", "-s3", "-n", "-Orientation", str(path)],
                              capture_output=True, text=True, timeout=60).stdout.strip()
        mirrored, degrees = ORIENTATIONS.get(int(said) if said.isdigit() else 1, (False, 0))
        degrees = (degrees + turn) % 360
        orientation = next(k for k, v in ORIENTATIONS.items() if v == (mirrored, degrees))
        args += [f"-Orientation#={orientation}"]
        if path.suffix.lower() in (".heic", ".heif"):
            args += [f"-QuickTime:Rotation={ROTATIONS[degrees]}"]
    else:
        args += ["-orientation"]
    done = subprocess.run([*args, "-o", "-", str(path)], capture_output=True, timeout=120)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError(f"exiftool could not copy {path.name}: {done.stderr.decode()[:200]}")
    left = json.loads(subprocess.run(["exiftool", "-j", "-G1", "-a", "-"], input=done.stdout,
                                     capture_output=True, timeout=60).stdout or b"[{}]")[0]
    if any(LOCATED.search(tag) for tag in left):
        raise RuntimeError(f"a location survived the copy of {path.name}")
    return done.stdout


@public.get("/{key}/{n}/original")
async def shared_original(key: str, n: int, session=Depends(get_session)):
    share = await _open(session, key)
    photo = _item(share, n)
    live = [f for f in photo.files if f.state == FILE_PRESENT]
    if not live:
        raise HTTPException(404, "the file is not where the catalogue left it")
    source = Path(live[0].path)
    copy = await asyncio.to_thread(clean_copy, source, photo.turn)
    name = f"{share.made_at:%Y-%m-%d}-{n + 1:03d}{source.suffix.lower()}"
    return Response(copy, media_type="application/octet-stream",
                    headers={**KEPT, "Content-Disposition": f'attachment; filename="{name}"'})
