"""Cataloguing photographs where they already are.

The other two halves adopt a library by identifying what a file is against an
outside catalogue. There is no outside catalogue for a family photograph — this
house is the only place it exists — so this pass asks a narrower question and
answers it exactly: is this file the one we already know, and is it still there.

NOTHING HERE WRITES UNDER THE LIBRARY ROOT. Not a rename, not a sidecar, not a
thumbnail. The pass opens files for reading and writes only database rows.

The shape follows the other two halves: one in-process pass started by
POST /api/photos/library/scan and by the heartbeat, its progress polled over GET,
no job table and no SSE.
"""

import asyncio
import datetime
import hashlib
import logging
import os
from pathlib import Path

from sqlalchemy import select, update

from opus.db import SessionLocal
from opus.models import (
    FILE_MISSING,
    FILE_PRESENT,
    FILE_QUARANTINED,
    Photo,
    PhotoFile,
    PhotoIntegrityEvent,
)
from opus.passes import Pass, Refused
from opus.photos.library import offers
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".avif", ".webp",
                    ".gif", ".tif", ".tiff", ".bmp",
                    ".cr2", ".cr3", ".arw", ".nef", ".dng", ".raf", ".orf", ".rw2"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv", ".3gp", ".mts", ".m2ts",
                    # an AVI that a 2005 camcorder's software named after the
                    # codec inside it rather than the container. Recognised
                    # here rather than renamed on disk: the file is what it is,
                    # and twenty-three of them are the only copy of an
                    # afternoon
                    ".divx"}

# Directories a photo tree grows that are derived rather than kept. Immich's
# thumbs and transcodes are the obvious case; they are its output, not the
# family's photographs, and the application that made them is being retired.
SKIP_DIRS = {"thumbs", "encoded-video", "profile", ".immich", "@eaDir",
             ".opus-incoming", "backups"}

# How much of the shelf may go missing in one pass before the pass stops
# believing itself. A mount that came back empty, a permissions change, a
# storage template rewrite part-way through: each of them looks exactly like a
# deletion, and tombstoning forty thousand photographs is not recoverable by a
# scan that runs again tomorrow.
MASS_MISSING_RATIO = 0.10
MASS_MISSING_FLOOR = 200  # below this many rows the ratio is noise, not a signal

HASH_CHUNK = 1024 * 1024

job = Pass("scan", total=0, processed=0, added=0, unchanged=0, moved=0, quarantined=0,
           missing=0, returned=0, hashed_bytes=0, current="")


def _kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def walk(root: Path) -> list[Path]:
    found: list[Path] = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in names:
            path = Path(base) / name
            if _kind(path):
                found.append(path)
    return sorted(found)


def _sha1(path: Path) -> tuple[bytes, int]:
    digest = hashlib.sha1()
    read = 0
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
            read += len(chunk)
    return digest.digest(), read


async def _note(session, kind: str, path: str = "", **detail) -> None:
    session.add(PhotoIntegrityEvent(kind=kind, path=path, detail=detail))


async def _photo_for(session, checksum: bytes, size: int, kind: str) -> Photo:
    photo = (await session.execute(
        select(Photo).where(Photo.checksum == checksum))).scalar_one_or_none()
    if photo is None:
        photo = Photo(checksum=checksum, byte_size=size, kind=kind)
        session.add(photo)
        # flushed first because the note is answered with the id it produces:
        # a vault has to be told which row in the library its picture became
        await session.flush()
        photo.offered_by = await offers.claimed_by(session, checksum, photo.id)
    return photo


async def _adopt(session, path: Path, st: os.stat_result, kind: str,
                 known: tuple | None) -> str:
    """Bring one file into the catalogue, and say what happened to it.

    The order of the questions is the whole cost model: the cheapest answer that
    is still true is taken first, and the file is only read when nothing cheaper
    can settle it."""
    state = job.state
    if known is not None:
        file_id, dev, inode, size, mtime_ns, file_state, checksum = known
        if (dev, inode, size, mtime_ns) == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns):
            # A file that was gone and is back is good news and clears itself.
            # A quarantine is not: it says the bytes under a known path stopped
            # being the bytes we cataloged, and the new stat was recorded so the
            # pass would stop rehashing it — not so the finding would expire.
            # Only a person can decide which of the two copies was the right one.
            if file_state == FILE_MISSING:
                await session.execute(update(PhotoFile).where(PhotoFile.id == file_id)
                                      .values(state=FILE_PRESENT, missing_since=None))
                await _note(session, "returned", str(path))
                return "returned"
            return "unchanged" if file_state == FILE_PRESENT else "quarantined"

        stat = {"dev": st.st_dev, "inode": st.st_ino,
                "byte_size": st.st_size, "mtime_ns": st.st_mtime_ns}
        # The path is known and the file underneath it is not the one we read.
        # This is the only case where the catalogue and the disk disagree about
        # content, and guessing which is right is how a library loses a
        # photograph quietly.
        now, _ = await asyncio.to_thread(_sha1, path)
        state["hashed_bytes"] += st.st_size
        if now != checksum:
            await session.execute(update(PhotoFile).where(PhotoFile.id == file_id)
                                  .values(state=FILE_QUARANTINED, **stat))
            await _note(session, "quarantined", str(path),
                        was=checksum.hex(), now=now.hex())
            return "quarantined"
        # Same bytes under the same path on a new inode: a copy back from
        # somewhere else, or a restore. The identity has to follow the file or
        # the next rename costs a full read again.
        await session.execute(update(PhotoFile).where(PhotoFile.id == file_id)
                              .values(state=FILE_PRESENT, missing_since=None, **stat))
        return "unchanged"

    # An unknown path on a known inode is the same file under a new name, which
    # is what a storage template change produces 45,000 times in one go. Size
    # and mtime have to agree as well: an inode number alone is recycled.
    moved = (await session.execute(select(PhotoFile).where(
        PhotoFile.dev == st.st_dev,
        PhotoFile.inode == st.st_ino,
        PhotoFile.byte_size == st.st_size,
        PhotoFile.mtime_ns == st.st_mtime_ns,
    ))).scalar_one_or_none()
    if moved is not None:
        await _note(session, "moved", str(path), was=moved.path)
        moved.path = str(path)
        moved.state, moved.missing_since = FILE_PRESENT, None
        return "moved"

    checksum, size = await asyncio.to_thread(_sha1, path)
    state["hashed_bytes"] += size
    photo = await _photo_for(session, checksum, size, kind)
    session.add(PhotoFile(
        photo_id=photo.id, path=str(path),
        dev=st.st_dev, inode=st.st_ino, byte_size=st.st_size, mtime_ns=st.st_mtime_ns,
        state=FILE_PRESENT,
    ))
    return "added"


async def _sweep(session, seen: set[str]) -> None:
    """Tombstone what was not found, unless too much of it was not found.

    A file that is gone is never deleted from the catalogue. Its names, its
    album membership and one day its faces are judgements a person made, and
    they must survive a disk being unplugged for an afternoon."""
    known = (await session.execute(
        select(PhotoFile.id, PhotoFile.path).where(PhotoFile.state == FILE_PRESENT)
    )).all()
    gone = [(fid, path) for fid, path in known if path not in seen]
    if not gone:
        return

    total = len(known)
    if total >= MASS_MISSING_FLOOR and len(gone) / total > MASS_MISSING_RATIO:
        await _note(session, "mass_missing", detail_gone=len(gone), detail_known=total)
        await session.commit()
        raise Refused(
            f"{len(gone)} of {total} files were not found; refusing to tombstone. "
            "Check the mount before running again.")

    now = datetime.datetime.now(datetime.UTC)
    await session.execute(
        update(PhotoFile)
        .where(PhotoFile.id.in_([fid for fid, _ in gone]))
        .values(state=FILE_MISSING, missing_since=now))
    for _, path in gone[:200]:
        await _note(session, "missing", path)
    job.state["missing"] = len(gone)


async def walk_and_adopt() -> None:
    state = job.state
    async with SessionLocal() as session:
        config = await current_runtime()
        root = Path(config.get("photos_dir"))

        # A root that cannot be read is not an empty library. Every other
        # guard in this module exists because this one can be wrong.
        if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            await _note(session, "root_unreadable", str(root))
            await session.commit()
            raise Refused(f"the photo library at {root} is not readable")

        state["phase"] = "walking"
        files = await asyncio.to_thread(walk, root)
        state["total"] = len(files)

        known = {row[0]: row[1:] for row in (await session.execute(
            select(PhotoFile.path, PhotoFile.id, PhotoFile.dev, PhotoFile.inode,
                   PhotoFile.byte_size, PhotoFile.mtime_ns, PhotoFile.state, Photo.checksum)
            .join(Photo, Photo.id == PhotoFile.photo_id))).all()}

        if not files and known:
            await _note(session, "mass_missing", str(root), detail_gone="all")
            await session.commit()
            raise Refused(f"{root} came back empty while the catalogue is not")

        state["phase"] = "reading"
        seen: set[str] = set()
        for path in files:
            state["current"] = path.name
            state["processed"] += 1
            try:
                st = await asyncio.to_thread(os.stat, path)
            except FileNotFoundError:
                # walked, then gone: the sweep will decide about it
                continue
            seen.add(str(path))
            try:
                outcome = await _adopt(session, path, st, _kind(path) or "image",
                                       known.get(str(path)))
                state[outcome] += 1
                if outcome != "unchanged":
                    await session.commit()
            except Exception as exc:
                await session.rollback()
                log.exception("cataloguing failed: %s", path)
                await _note(session, "error", str(path), message=str(exc))
                await session.commit()

        state["phase"] = "sweeping"
        await _sweep(session, seen)
        await session.commit()
