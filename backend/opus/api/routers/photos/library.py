"""The photo library as it is on disk: what a pass found, what the whole of it
adds up to, and what the guards refused to do.

There is no adoption question here of the kind the video half asks. A film has
to be identified against TMDB before it means anything; a photograph means what
it is. So the only judgements a pass reports are about existence — this is the
file we knew, it moved, it is gone, its contents changed under us."""

import asyncio
import datetime
import hashlib
import logging
import secrets
from pathlib import Path
from typing import Literal

import opus_auth
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from opus_core.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from opus import auth
from opus.api.routers.photos.shared import unmoved
from opus.api.routers.photos.timeline import _shape
from opus.db import get_session
from opus.models import (
    FACE_GENERATION,
    FILE_MISSING,
    FILE_PRESENT,
    FILE_QUARANTINED,
    Face,
    Photo,
    PhotoFile,
)
from opus.photos import pipeline, room
from opus.photos.library import derive, metadata, offers, scan
from opus.photos.people import cluster, focus
from opus.settings_store import current_runtime
from opus.video.subtitles.probe import ProbeError, probe_file

log = logging.getLogger(__name__)
router = APIRouter()


async def offering(request: Request,
                   session: AsyncSession = Depends(get_session)) -> str:
    """Who is giving this picture to the household.

    A name is asked for rather than allowed to be absent, because a photograph
    in the shared library carries who put it there and an anonymous one would
    be a picture nobody could be asked about."""
    person = auth.whoami(await auth.roster(session),
                         request.cookies.get(opus_auth.SESSION_COOKIE))
    if person is None:
        raise HTTPException(403, "a picture is given by somebody, and none is signed in")
    return person


@router.post("/offer")
async def offer(request: Request, name: str = Query(""),
                person: str = Depends(offering),
                session: AsyncSession = Depends(get_session)):
    """A picture handed straight to the household, from a device rather than
    from a vault.

    The vault is for keeping and this is for giving, and the second must not
    wait on the first: somebody who will never want a private corner still has
    the evening's photographs everyone else is asking for. What arrives is
    written into the same waiting room a vault offers into and adopted by the
    same pass, because a second way into the catalogue would be a second set of
    rules to disagree with the first.

    Streamed to disk as it comes and read for its checksum on the way, so a
    video is never held whole in memory."""
    config = await current_runtime()
    waiting = offers.waiting_room(config)
    most = room.spare(waiting)
    announced = request.headers.get("content-length", "")
    if announced.isdigit() and int(announced) > room.LARGEST:
        raise HTTPException(413, "a file this large is not taken")
    if most <= 0 or (announced.isdigit() and int(announced) > most):
        raise HTTPException(507, "there is not enough room left in the library")

    # a name the pass will not adopt: what this is is not known until it has
    # been read to the end, and a half-written file is not a photograph
    working = waiting / f".{secrets.token_hex(8)}.arriving"
    digest = hashlib.sha1()
    size = 0
    try:
        with working.open("wb") as handle:
            async for piece in request.stream():
                size += len(piece)
                if size > room.LARGEST:
                    raise HTTPException(413, "a file this large is not taken")
                if size > most:
                    raise HTTPException(507, "there is not enough room left in the library")
                digest.update(piece)
                await asyncio.to_thread(handle.write, piece)
        if not size:
            raise HTTPException(400, "nothing was sent")

        checksum = digest.digest()
        said = await offers.already(session, checksum, person, None)
        if said is not None:
            return said
        landed = await offers.give(session, working, config, name, checksum, person, None)
    finally:
        working.unlink(missing_ok=True)

    if landed is None:
        return {"known": True, "pending": True}
    return {"known": False, "at": str(landed), "bytes": size}


@router.post("/library/scan", status_code=202)
async def library_scan_start():
    if not scan.job.start(scan.walk_and_adopt):
        raise HTTPException(status_code=409, detail="scan already running")
    return {"status": "started"}


@router.get("/library/scan")
async def library_scan_status():
    return scan.job.state


@router.post("/library/metadata", status_code=202)
async def library_metadata(rescan: bool = False):
    """Read what the cameras wrote down. `rescan` re-asks photographs that
    already have an answer, which is what a corrected timezone or a better
    filename rule needs."""
    if not metadata.job.start(metadata.read, rescan):
        raise HTTPException(status_code=409, detail="metadata pass already running")
    return {"status": "started"}


@router.get("/library/metadata")
async def library_metadata_status():
    return metadata.job.state


@router.post("/library/derive", status_code=202)
async def library_derive(regenerate: bool = False, retry_failed: bool = False):
    """Make the tile, the preview and the thumbhash.

    `regenerate` re-makes what is already current, which is what a changed
    recipe needs. `retry_failed` re-reads only the photographs this recipe could
    not decode — which is what a newer decoder needs, and is two files rather
    than forty-one thousand."""
    if not derive.job.start(derive.make, regenerate, retry_failed):
        raise HTTPException(status_code=409, detail="derivation already running")
    return {"status": "started"}


@router.get("/library/derive")
async def library_derive_status():
    return derive.job.state


@router.delete("/library/derive", status_code=202)
async def library_derive_cancel():
    """Stop at the next batch. What is already written stays written — the
    derivatives are addressed by content, so a pass that resumes picks up
    exactly where this one left off."""
    if not derive.job.cancel():
        raise HTTPException(status_code=409, detail="nothing running")
    return {"status": "stopping"}


@router.get("/{checksum}/tile")
async def photo_tile(checksum: str, session=Depends(get_session)):
    return await _serve(session, checksum, 0)


@router.get("/{checksum}/preview")
async def photo_preview(checksum: str, session=Depends(get_session)):
    return await _serve(session, checksum, 1)


# what a browser will play without being asked twice
PLAYS = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
         ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
         ".wmv": "video/x-ms-asf", ".3gp": "video/3gpp"}


async def _recording(session, checksum: str) -> Path:
    try:
        digest = bytes.fromhex(checksum)
    except ValueError:
        raise HTTPException(400, "not a checksum")
    photo = (await session.execute(
        select(Photo).options(selectinload(Photo.files))
        .where(Photo.checksum == digest))).scalar_one_or_none()
    if photo is None:
        raise HTTPException(404, "no such photograph")
    live = [f for f in photo.files if f.state == FILE_PRESENT]
    if not live:
        raise HTTPException(404, "the file is not where the catalogue left it")
    return Path(live[0].path)


@router.get("/{checksum}/play")
async def play(checksum: str, session=Depends(get_session)):
    """The recording itself, for a player to scrub through.

    The shelf holds five hundred and forty-eight of these and had no way to hand
    one over — they were dated, placed, counted and unwatchable. Served whole and
    untranscoded: it is the household's own file on the household's own disk.
    What a screen cannot open as it is, the player rebuilds from /playback.

    Ranges are the point. Without them a player must have the whole file before
    it can show the middle of it, which for a minute of 4K is a wait nobody sits
    through — and FileResponse answers them itself."""
    path = await _recording(session, checksum)
    return FileResponse(
        path, media_type=PLAYS.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/{checksum}/playback")
async def playback(checksum: str, session=Depends(get_session)):
    """What the recording is, in the shape a film's playback carries, for a
    player that has to decide whether a screen takes it as it is. Read from the
    file when asked: a household clip is read in a moment, and a DivX from 2005
    or an iPhone's HEVC with PCM sound is not something a browser opens."""
    path = await _recording(session, checksum)
    try:
        info = await probe_file(path)
    except ProbeError as exc:
        raise HTTPException(422, str(exc))
    return {"path": str(path), **{key: info[key] for key in (
        "container", "video_codec", "width", "height", "duration_s", "streams")}}


async def _serve(session, checksum: str, which: int):
    """A derivative that is not there is a 404 that says which one and why —
    never an empty response that looks like an empty library.

    But it says so only when there is nothing to send. A screen of photographs is
    a hundred and twenty of these, and asking the catalogue whether each one
    exists before handing over a file whose name is already the answer was a
    hundred and twenty round trips to serve pictures off the local disk. The
    derivative is named by the digest, so the file being there IS the proof that
    the photograph is; the database is consulted when it is not, which is the
    only time anybody needs a sentence rather than an image."""
    try:
        digest = bytes.fromhex(checksum)
    except ValueError:
        raise HTTPException(400, "not a checksum")
    config = await current_runtime()
    path = derive.paths_for(derive.store_root(config), digest)[which]
    if path.exists():
        return FileResponse(path, media_type="image/jpeg" if which == 0 else "image/avif",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    photo = (await session.execute(
        select(Photo).where(Photo.checksum == digest))).scalar_one_or_none()
    if photo is None:
        raise HTTPException(404, "no such photograph")
    raise HTTPException(404, "not derived yet" if not photo.derived_at
                        else "derivative missing from the store")


@router.get("/library")
async def library_summary(session=Depends(get_session)):
    """What is shelved, counted in the database rather than in Python.

    Missing and quarantined are reported beside present rather than subtracted
    from it: a library that quietly shows a smaller number is a library that has
    stopped telling you something went wrong."""
    files = dict((await session.execute(
        select(PhotoFile.state, func.count()).group_by(PhotoFile.state))).all())
    kinds = dict((await session.execute(
        select(Photo.kind, func.count()).group_by(Photo.kind))).all())
    bytes_held = (await session.execute(
        select(func.coalesce(func.sum(Photo.byte_size), 0)))).scalar_one()
    dated = dict((await session.execute(
        select(Photo.taken_source, func.count()).group_by(Photo.taken_source))).all())
    derived = (await session.execute(select(func.count()).select_from(Photo)
                                      .where(Photo.derived_gen == derive.GENERATION))).scalar_one()
    underivable = (await session.execute(
        select(func.count()).select_from(Photo)
        .where(Photo.derive_failed_gen == derive.GENERATION))).scalar_one()
    duplicated = (await session.execute(
        select(func.count()).select_from(
            select(PhotoFile.photo_id).group_by(PhotoFile.photo_id)
            .having(func.count() > 1).subquery()))).scalar_one()
    return {
        "photographs": sum(kinds.values()),
        "images": kinds.get("image", 0),
        "videos": kinds.get("video", 0),
        "bytes": bytes_held,
        "files": {
            "present": files.get(FILE_PRESENT, 0),
            "missing": files.get(FILE_MISSING, 0),
            "quarantined": files.get(FILE_QUARANTINED, 0),
        },
        # one photograph found at more than one path — worth knowing before
        # anyone offers to reclaim the space, and never resolved automatically
        "duplicated": duplicated,
        # how much of the shelf can say when it was taken, and on whose word
        "dated": dated,
        # and how much of it can be looked at without opening the original
        "derived": derived,
        # photographs this recipe cannot read at all. Reported beside `derived`
        # for the same reason missing is reported beside present: a number that
        # is quietly smaller has stopped telling you something went wrong.
        "underivable": underivable,
    }


@router.get("/library/underivable")
async def library_underivable(session=Depends(get_session)):
    """The photographs the current recipe cannot decode, with the decoder's own
    words and where they are.

    Two files out of 41,012 is small enough to list and too small to notice any
    other way."""
    rows = (await session.execute(
        select(Photo).options(selectinload(Photo.files))
        .where(Photo.derive_failed_gen == derive.GENERATION)
        .order_by(Photo.id))).scalars().all()
    return [{
        "id": p.checksum.hex(),
        "error": p.derive_error,
        "bytes": p.byte_size,
        "pixels": [p.pixel_w, p.pixel_h],
        "device": " ".join(x for x in (p.device_make, p.device_model) if x),
        "taken_at": p.taken_at.isoformat() if p.taken_at else None,
        "paths": [f.path for f in p.files],
    } for p in rows]


@router.get("/library/keeping-up")
async def library_keeping_up():
    """What the photograph half is doing, and what it last did."""
    return pipeline.state


@router.post("/library/focus", status_code=202)
async def library_focus(remeasure: bool = False):
    """Measure how sharp every face is.

    A face out of focus still embeds, and it embeds near every other face out of
    focus — so without this, everyone in the archive who was ever mis-focused
    assembles into a single person made of smears, complete with a birth year
    worked out from faces nobody can see."""
    if not focus.job.start(focus.measure, remeasure):
        raise HTTPException(status_code=409, detail="focus pass already running")
    return {"status": "started"}


@router.get("/library/focus")
async def library_focus_status(session=Depends(get_session)):
    cut = (await current_runtime()).float("faces_min_sharpness")
    return {**focus.job.state, "shelf": await focus.shelf(session, cut)}


@router.delete("/library/focus", status_code=200, dependencies=[Depends(unmoved)])
async def library_focus_drop(session=Depends(get_session)):
    """Throw away the faces nobody could recognise.

    They are not evidence of anything: a smear cannot be named, cannot be dated,
    and drags whatever it is grouped with toward every other smear. Deleting is
    safe in the way it usually is not — the pass that found them will find them
    again, so this is a decision that can be taken back by running it."""
    cut = (await current_runtime()).float("faces_min_sharpness")
    groups = list((await session.execute(
        select(Face.cluster_id).distinct()
        .where(Face.generation == FACE_GENERATION, Face.sharpness < cut,
               Face.cluster_id.is_not(None)))).scalars())
    gone = (await session.execute(
        delete(Face).where(Face.generation == FACE_GENERATION,
                           Face.sharpness < cut)
        .returning(Face.id))).all()
    await cluster.drop_empty(session)
    await cluster.refresh_centroids(session, groups)
    await session.commit()
    return {"deleted": len(gone), "cut": cut}


@router.delete("/{checksum}", dependencies=[Depends(unmoved)])
async def forget(checksum: str, session: AsyncSession = Depends(get_session)):
    """Take a photograph out of the library for good.

    An admin's, without a word here saying so: every request that changes
    something is already the admin's unless its path is named in USERS_MAY, and
    that rule fails closed. Naming it a second time in this module would be a
    second place for the two to disagree.

    The file goes with the row, because the row alone would come straight back —
    the next pass walks the tree and adopts whatever it finds there. There is no
    bin: the tree is snapshotted nightly and sent on, so what undoes this is the
    same thing that undoes everything else, and a second copy kept here would be
    a second copy to keep honest.

    What is NOT touched is anybody's vault. The picture may be somebody's own
    backup as well as the household's, and the two are separate stores on
    purpose — the link between them is dropped, not followed.
    """
    try:
        raw = bytes.fromhex(checksum)
    except ValueError:
        raise HTTPException(400, "that is not a checksum")

    photo = (await session.execute(
        select(Photo).options(selectinload(Photo.files))
        .where(Photo.checksum == raw))).scalar_one_or_none()
    if photo is None:
        raise HTTPException(404, "the library has no such photograph")

    config = await current_runtime()
    read_root = Path(config.get("photos_dir"))
    write_root = Path(config.get("photos_write_dir"))
    groups = list((await session.execute(
        select(Face.cluster_id).distinct()
        .where(Face.photo_id == photo.id, Face.cluster_id.is_not(None)))).scalars())

    gone = []
    for row in photo.files:
        # the catalogue only ever speaks the read-only name, and only the
        # writable one can be unlinked
        try:
            where = write_root / Path(row.path).relative_to(read_root)
        except ValueError:
            log.warning("photo %s sits outside the tree at %s", photo.id, row.path)
            continue
        try:
            where.unlink()
            gone.append(row.path)
        except FileNotFoundError:
            # already absent is the outcome asked for
            gone.append(row.path)
        except OSError as why:
            raise HTTPException(500, f"could not remove {row.path}: {why}")

    for spare in derive.paths_for(derive.store_root(config), raw):
        spare.unlink(missing_ok=True)

    await session.delete(photo)
    await session.flush()
    await cluster.drop_empty(session)
    await cluster.refresh_centroids(session, groups)
    await session.commit()
    log.info("photo %s removed: %s", checksum[:12], ", ".join(gone) or "no file")
    return {"removed": gone}


class Turn(BaseModel):
    by: Literal[90, 180, 270]


@router.post("/{checksum}/turn", dependencies=[Depends(unmoved)])
async def turn(checksum: str, asked: Turn, session: AsyncSession = Depends(get_session)):
    """Turn a photograph clockwise, for the file that recorded itself lying down.

    The original is not touched: the turn is the catalogue's, and the tile and
    the preview are drawn again with it before this answers, so the picture on
    the screen is the turned one. Its faces are dropped and looked for again —
    a face on its side is one the detector mostly missed and the encoder
    measured wrong, and a box kept from the old frame would point at nothing."""
    try:
        raw = bytes.fromhex(checksum)
    except ValueError:
        raise HTTPException(400, "that is not a checksum")
    photo = (await session.execute(
        select(Photo).options(selectinload(Photo.files))
        .where(Photo.checksum == raw))).scalar_one_or_none()
    if photo is None:
        raise HTTPException(404, "the library has no such photograph")
    if photo.kind != "image":
        raise HTTPException(422, "only a still photograph is turned")
    live = [f for f in photo.files if f.state == FILE_PRESENT]
    if not live:
        raise HTTPException(409, "the photograph has no file to draw it from")

    photo.turn = (photo.turn + asked.by) % 360
    if asked.by != 180:
        photo.pixel_w, photo.pixel_h = photo.pixel_h, photo.pixel_w
    config = await current_runtime()
    root = derive.store_root(config)
    tile, preview = derive.paths_for(root, raw)
    try:
        await asyncio.to_thread(derive.prepare_store, root)
        _, digest = await asyncio.to_thread(
            derive._write, Path(live[0].path), tile, preview, root / ".incoming",
            False, photo.turn)
    except Exception as why:
        raise HTTPException(500, f"could not draw the turned photograph: {why}")
    photo.thumbhash = digest
    photo.derived_at = datetime.datetime.now(datetime.UTC)
    photo.derived_gen = derive.GENERATION

    groups = list((await session.execute(
        select(Face.cluster_id).distinct()
        .where(Face.photo_id == photo.id, Face.cluster_id.is_not(None)))).scalars())
    await session.execute(delete(Face).where(Face.photo_id == photo.id))
    photo.faces_gen = 0
    await session.flush()
    await cluster.drop_empty(session)
    await cluster.refresh_centroids(session, groups)
    await session.commit()
    log.info("photo %s turned to %d", checksum[:12], photo.turn)
    return _shape(photo)
