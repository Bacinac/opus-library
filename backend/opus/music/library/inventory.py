"""Folder inventory: the disk tree becomes `files` rows. Walks the library for
album folders, probes their audio files (skipping a folder whose size+mtime are
unchanged), upserts a row per path and reports the folder's tags. Local disk
and DB only — no network, and no scan-report writing: the caller owns the
verdict."""

import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from opus.db import SessionLocal
from opus.models import MusicFile
from opus.music.tagging.tagger import AUDIO_EXTENSIONS, probe_file

# every path the running scan has visited; sweeps.sweep_missing_files reads it
# to tell a file that vanished from one the scan simply never reached
seen_paths: set[str] = set()


def collect_album_dirs(root: Path) -> dict[Path, list[Path]]:
    dirs: dict[Path, list[Path]] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            dirs.setdefault(path.parent, []).append(path)
    return {d: sorted(files) for d, files in sorted(dirs.items())}


def _probe_files(files: list[Path]) -> list[tuple[Path, dict]]:
    return [(path, probe_file(path)) for path in files]


def _stat_files(files: list[Path]) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        out[str(path)] = (stat.st_size, stat.st_mtime)
    return out


async def inventory_folder(files: list[Path]) -> tuple[str | None, str | None]:
    """Phase 1: probe the folder's files (skipping an unchanged folder via the
    mtime stat sweep), upsert their rows, and return (tag_artist, tag_album).
    Returns (None, None) when the folder has no usable tags — the caller
    records that as 'no_tags'. No network — local disk + DB only."""
    paths = [str(p) for p in files]
    seen_paths.update(paths)
    stats = await asyncio.to_thread(_stat_files, files)
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(MusicFile).where(MusicFile.path.in_(paths))
            .order_by(MusicFile.path)
        )).scalars().all()
        by_path = {r.path: r for r in rows}
        unchanged = len(by_path) == len(paths) and all(
            path in stats
            and by_path[path].size == stats[path][0]
            and by_path[path].mtime == stats[path][1]
            # a row missing its duration, its channel layout or the tags the
            # file carries (probed before any of them was read) must re-probe
            # once to backfill it, even when size+mtime are unchanged
            and by_path[path].duration_sec is not None
            and by_path[path].channels is not None
            and by_path[path].tags is not None
            for path in paths
        )
        if not unchanged:
            infos = await asyncio.to_thread(_probe_files, files)
            for path, info in infos:
                await session.execute(
                    insert(MusicFile)
                    .values(path=str(path), **info)
                    .on_conflict_do_update(index_elements=["path"],
                                           set_=info or {"size": None})
                )
            await session.commit()
            rows = (await session.execute(
                select(MusicFile).where(MusicFile.path.in_(paths))
                .order_by(MusicFile.path)
            )).scalars().all()

        tag_artist = next((r.tag_artist for r in rows if r.tag_artist), None)
        tag_album = next((r.tag_album for r in rows if r.tag_album), None)
    if not tag_artist or not tag_album:
        return None, None
    return tag_artist, tag_album
