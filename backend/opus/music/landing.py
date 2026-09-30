"""Which folders in the landing zone are copies of records already on the shelf.

A folder whose every song is ALREADY on a shelf is a copy of something we hold,
and copies are what the landing zone is for on the way in, not after — so it
goes without waiting for the clock. Identity is what the files say, artist and
album off their own tags, because the importer renames the file and rewrites its
tags, so nothing outside them survives the trip.
"""

import asyncio
import logging
import re
import unicodedata
from pathlib import Path

from sqlalchemy import select

from opus import landing
from opus.db import SessionLocal
from opus.models import Artist, MusicFile, Release, Track
from opus.music.tagging import tagger

log = logging.getLogger("opus.landing")

_PUNCT = re.compile(r"[^\w\s]|_")


def _one(value):
    """A FLAC field may repeat, and mutagen hands those back as a list."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value or ""


def _key(text) -> str:
    text = unicodedata.normalize("NFKD", str(_one(text)).lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(_PUNCT.sub(" ", text).split())


async def _shelved() -> set[tuple[str, str]]:
    """Every artist+album the shelf actually holds files for."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Artist.name, Release.title)
            .select_from(Release)
            .join(Artist, Artist.id == Release.artist_id)
            .join(Track, Track.release_id == Release.id)
            .join(MusicFile, MusicFile.track_id == Track.id)
            .group_by(Artist.name, Release.title)
        )).all()
    return {(_key(a), _key(t)) for a, t in rows}


def _says(folder: Path) -> set[tuple[str, str]]:
    """Which records the audio under this folder claims to be.

    Under, not in: an engine may lay a folder out by artist with the albums
    inside it — a store's does — and a check that only looked at the top level saw
    no audio at all and concluded nothing. A couple of files per record settle
    it; reading a whole box set to learn its name is work for nothing."""
    said = set()
    audio = [f for f in sorted(folder.rglob("*"))
             if f.is_file() and f.suffix.lower() in tagger.AUDIO_EXTENSIONS]
    # one sample per directory, so a folder of many albums is judged on all of
    # them rather than on whichever three sort first
    by_dir: dict[Path, list[Path]] = {}
    for f in audio:
        by_dir.setdefault(f.parent, []).append(f)
    for group in by_dir.values():
        for one in group[:2]:
            try:
                tags = tagger.probe_file(one).get("tags") or {}
            except Exception as exc:
                log.warning("landing sweep: %s could not be read: %s", one, exc)
                continue
            artist = _one(tags.get("ALBUMARTIST") or tags.get("ARTIST"))
            album = _one(tags.get("ALBUM"))
            if artist and album:
                said.add((_key(artist), _key(album)))
    return said


async def sweep_copies() -> None:
    where = await landing.root()
    if where is None:
        return
    shelf = await _shelved()
    held = freed = 0
    for folder in await asyncio.to_thread(landing.candidates, where):
        try:
            said = await asyncio.to_thread(_says, folder)
        except OSError:
            continue
        if said and said <= shelf:
            freed += await landing.remove(folder, "copy of the shelf")
            held += 1
    if held:
        log.info("landing sweep: %d already held, %.1f GB", held, freed / 1024 ** 3)
