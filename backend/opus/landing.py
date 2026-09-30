"""Keeping the landing zone from filling up.

A finished job's folder is removed when its files reach a shelf — that is
`cleanup_after_import`, and it works. What it never covered is everything that
does NOT reach one: an import refused because the catalogue row wants eighty-
eight tracks and the album has twelve, a transfer the backend restarted under, a
grab nobody ever looked at again. Those stay forever, and forever came to a
hundred and twenty gigabytes.

This is a clock and nothing else: a folder that has sat untouched longer than
the keep is one nobody is coming back for. It deliberately knows nothing about
why it failed, because the reasons are many and the answer is the same. A folder
that is already a copy of something on a shelf needs no clock, and saying which
those are is each half's own business.

What is never touched: the root, the per-engine and per-namespace folders that
divide it, `.incomplete`, and anything younger than the keep.
"""

import asyncio
import datetime
import logging
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from opus import schedule
from opus.db import SessionLocal
from opus.settings_store import current_runtime

log = logging.getLogger("opus.landing")

# the divisions of the zone itself: engines and namespaces, never their contents
_KEEP_WHOLE = {".incomplete", "movies", "music", "newsgroup", "real_debrid",
               "series", "soulseek", "stream", "torrent", "tv",
               "video", "videos", "complete", "incomplete"}


def _idle_days(folder: Path) -> float:
    """How long since anything in it last changed — the folder's own mtime is
    not enough, since a file can be written into it without touching it."""
    newest = folder.stat().st_mtime
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(Path(root, name)))
            except OSError:
                continue
    return (time.time() - newest) / 86400


def candidates(root: Path) -> list[Path]:
    """Every folder that holds a job's files, and no folder that divides the
    zone. Engine and namespace divisions can be nested, so walk through every
    one rather than stopping after the first level."""
    out: list[Path] = []

    def beneath(parent: Path) -> None:
        for child in sorted(parent.iterdir()):
            # A landing zone is supplied by download engines, so never follow a
            # symlink out of it while deciding what maintenance may remove.
            if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
                continue
            if child.name.lower() in _KEEP_WHOLE:
                beneath(child)
            else:
                out.append(child)

    beneath(root)
    return out


def _size(folder: Path) -> int:
    """The reclaimable bytes below one candidate.

    This deliberately has the same definition as ``remove``. A file can
    disappear while an engine settles its bookkeeping; that makes a preview
    slightly stale, never a reason to make the preview fail.
    """
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def without_active(candidates: list[Path], active: set[Path]) -> list[Path]:
    """Candidates no current download or import can still be using.

    Engines do not all report a landing path at the same level.  Protect both a
    job directory and its parent/child relationship to a candidate: deleting
    either side could take files away from an in-flight job.
    """
    return [folder for folder in candidates if not any(
        folder == held or folder in held.parents or held in folder.parents for held in active)]


async def active_folders(session, where: Path) -> set[Path]:
    """Landing directories named by downloads that have not settled yet."""
    # Imported lazily: landing is started with the application, while these
    # models import the media pipelines that also start it.
    from opus.models import MusicDownload, MusicDownloadStatus, VideoDownload

    music = await session.scalars(select(MusicDownload.job_ref).where(
        MusicDownload.status.in_((MusicDownloadStatus.QUEUED,
                                  MusicDownloadStatus.DOWNLOADING,
                                  MusicDownloadStatus.IMPORTING))))
    video = await session.scalars(select(VideoDownload.job_ref).where(
        VideoDownload.state.in_(("queued", "downloading", "downloaded"))))
    root = where.resolve()
    held: set[Path] = set()
    for ref in (*music.all(), *video.all()):
        path = (ref or {}).get("directory") or (ref or {}).get("landing")
        if not isinstance(path, str) or not path:
            continue
        candidate = Path(path).resolve()
        if candidate == root or root in candidate.parents:
            held.add(candidate)
    return held


async def root() -> Path | None:
    config = await current_runtime()
    where = Path(config.get("opus_landing_dir"))
    if not where.is_dir():
        log.warning("landing sweep: %s is not a directory", where)
        return None
    return where


async def remove(folder: Path, why: str, idle: float | None = None) -> int:
    size = await asyncio.to_thread(_size, folder)
    log.info("landing sweep: %s %s (%.1f GB%s)", why, folder, size / 1024 ** 3,
             f", idle {idle:.0f} d" if idle is not None else "")
    await asyncio.to_thread(shutil.rmtree, folder, True)
    return size


async def preview(where: Path, keep: float, active: set[Path]) -> dict:
    """Describe a sweep without changing the landing zone.

    The report is intentionally made on demand rather than in the periodic
    sweeper: walking old transfers can touch many disks. Paths are relative to
    the landing root so the UI is useful without exposing host mount details.
    """
    found = await asyncio.to_thread(candidates, where)
    eligible = without_active(found, active)
    stale: list[dict] = []
    for folder in eligible:
        try:
            idle = await asyncio.to_thread(_idle_days, folder)
        except OSError:
            continue
        if idle < keep:
            continue
        stale.append({
            "path": str(folder.relative_to(where)),
            "idle_days": round(idle, 1),
            "bytes": await asyncio.to_thread(_size, folder),
        })
    stale.sort(key=lambda item: (-item["bytes"], item["path"]))
    return {
        "root": str(where),
        "keep_days": keep,
        "candidates": len(found),
        "protected": len(found) - len(eligible),
        "stale": len(stale),
        "reclaimable": sum(item["bytes"] for item in stale),
        # A very large abandoned tree should still be inspectable without
        # making one response unbounded. Totals above always cover every item.
        "entries": stale[:100],
        "truncated": len(stale) > 100,
    }


async def sweep_once() -> None:
    where = await root()
    if where is None:
        return
    keep = (await current_runtime()).float("landing_keep_days")
    async with SessionLocal() as session:
        active = await active_folders(session, where)
    stale = freed = 0
    for folder in without_active(await asyncio.to_thread(candidates, where), active):
        try:
            idle = await asyncio.to_thread(_idle_days, folder)
        except OSError:
            continue
        if idle < keep:
            continue
        freed += await remove(folder, "past its keep", idle)
        stale += 1
    if stale:
        log.info("landing sweep: %d past the keep, %.1f GB", stale, freed / 1024 ** 3)


async def sweep_loop():
    await schedule.every("landing.sweep", datetime.timedelta(hours=6), sweep_once)
