"""The catalogue's pictures, taken before anybody asks for them.

Three steps, each cheap to repeat. Every picture the catalogue names is
registered. Every record, film and series the library holds is looked at on
the disk, and a picture found beside it becomes that picture's original — a
cover somebody picked by hand excepted, because the file beside the record is
the one the tagger wrote before the pick. Then what is still wanted is taken,
one host at a time, at the pace each host tolerates."""

import asyncio
import datetime
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from opus import schedule
from opus.db import SessionLocal
from opus.models import (Artist, Episode, Image, Movie, MusicFile, Picture, Release, Season,
                         Series, Track, VideoFile)
from opus.models.pictures import HELD
from opus.passes import Pass
from opus.pictures import store

log = logging.getLogger("opus.pictures")

INTERVAL = datetime.timedelta(hours=6)
BATCH = 500

job = Pass("pictures", registered=0, attached=0, taken=0)

_SLOTS = """
    SELECT cover_url FROM releases
    UNION ALL SELECT image_url FROM artists
    UNION ALL SELECT poster_url FROM movies
    UNION ALL SELECT backdrop_url FROM movies
    UNION ALL SELECT poster_url FROM series
    UNION ALL SELECT backdrop_url FROM series
    UNION ALL SELECT poster_url FROM seasons
    UNION ALL SELECT still_url FROM episodes
    UNION ALL SELECT thumb_url FROM web_channels
    UNION ALL SELECT thumb_url FROM web_videos
    UNION ALL SELECT poster_url FROM award_titles
    UNION ALL SELECT logo FROM radio_stations
"""
_FACES = " UNION ALL ".join(
    f"SELECT jsonb_path_query({column}, 'strict $[*].{key}') #>> '{{}}' FROM {table}"
    for table in ("movies", "series")
    for column, key in (('"cast"', "profile_url"), ("directors", "profile_url"), ("studios", "logo")))

REGISTER = text(f"""
    INSERT INTO pictures (source)
    SELECT DISTINCT s FROM ({_SLOTS} UNION ALL {_FACES}) AS named(s)
    WHERE s LIKE 'http%' OR s LIKE '/%'
    ON CONFLICT (source) DO NOTHING
""")

PICTURES = (".jpg", ".jpeg", ".png", ".webp")
COVER = ("cover", "folder", "front")
PORTRAIT = ("artist", "folder")
POSTER = ("poster", "folder")
BACKDROP = ("fanart", "backdrop", "background")
# a record in parts keeps its cover a folder up from the parts
DISC = re.compile(r"^(cd|dis[ck])\s*\d+$", re.IGNORECASE)
SEASON = re.compile(r"^(season\s*\d+|specials)$", re.IGNORECASE)


def _listing(folder: Path, seen: dict[Path, dict[str, str]]) -> dict[str, str]:
    if folder not in seen:
        try:
            seen[folder] = {entry.name.lower(): entry.name for entry in folder.iterdir()
                            if entry.is_file()}
        except OSError:
            seen[folder] = {}
    return seen[folder]


def _named(folder: Path, stems: tuple[str, ...], seen: dict) -> str | None:
    names = _listing(folder, seen)
    for stem in stems:
        for suffix in PICTURES:
            if (real := names.get(stem + suffix)) is not None:
                return str(folder / real)
    return None


def _belongs(folder: Path) -> bool:
    """A folder of one record or one film, never a tree's root."""
    return str(folder).rstrip("/") + "/" not in store.LIBRARY_TREES and len(folder.parts) > 2


def _music(rows: list[tuple[int, int, str]], seen: dict) -> tuple[dict, dict]:
    covers: dict[int, str] = {}
    homes: dict[int, Counter] = defaultdict(Counter)
    folders: dict[int, set[Path]] = defaultdict(set)
    artist_of: dict[int, int] = {}
    for release_id, artist_id, path in rows:
        folder = Path(path).parent
        if DISC.match(folder.name):
            folder = folder.parent
        folders[release_id].add(folder)
        artist_of[release_id] = artist_id
    for release_id, found in folders.items():
        for folder in sorted(found):
            if _belongs(folder) and (cover := _named(folder, COVER, seen)):
                covers[release_id] = cover
                break
        for folder in found:
            if _belongs(folder.parent):
                homes[artist_of[release_id]][folder.parent] += 1
    portraits = {}
    for artist_id, counted in homes.items():
        home = counted.most_common(1)[0][0]
        if portrait := _named(home, PORTRAIT, seen):
            portraits[artist_id] = portrait
    return covers, portraits


def _films(rows: list[tuple[int, str]], seen: dict) -> tuple[dict, dict]:
    posters, backdrops = {}, {}
    for movie_id, path in rows:
        folder = Path(path).parent
        if not _belongs(folder):
            continue
        if poster := _named(folder, POSTER, seen):
            posters[movie_id] = poster
        if backdrop := _named(folder, BACKDROP, seen):
            backdrops[movie_id] = backdrop
    return posters, backdrops


def _season_stems(number: int) -> tuple[str, ...]:
    return ("season-specials-poster",) if number == 0 else (f"season{number:02d}-poster",)


def _television(rows: list[tuple[int, int, int, int, str]], seen: dict) -> tuple[dict, dict, dict, dict]:
    posters, backdrops, seasons, stills = {}, {}, {}, {}
    for episode_id, season_id, number, series_id, path in rows:
        file = Path(path)
        if still := _named(file.parent, (f"{file.stem}-thumb",), seen):
            stills[episode_id] = still
        show = file.parent.parent if SEASON.match(file.parent.name) else file.parent
        if not _belongs(show):
            continue
        if series_id not in posters and (poster := _named(show, POSTER, seen)):
            posters[series_id] = poster
        if series_id not in backdrops and (backdrop := _named(show, BACKDROP, seen)):
            backdrops[series_id] = backdrop
        if season_id not in seasons:
            found = _named(show, _season_stems(number), seen)
            if found is None and show != file.parent:
                found = _named(file.parent, POSTER, seen)
            if found:
                seasons[season_id] = found
    return posters, backdrops, seasons, stills


async def _owned():
    async with SessionLocal() as session:
        music = (await session.execute(
            select(Track.release_id, Release.artist_id, MusicFile.path)
            .join(Track, MusicFile.track_id == Track.id)
            .join(Release, Track.release_id == Release.id))).all()
        manual = {(r.entity_type, r.entity_id) for r in (await session.execute(
            select(Image.entity_type, Image.entity_id).where(Image.chosen_manual))).all()}
        films = (await session.execute(
            select(VideoFile.movie_id, VideoFile.path).where(VideoFile.movie_id.isnot(None)))).all()
        television = (await session.execute(
            select(Episode.id, Season.id, Season.number, Season.series_id, VideoFile.path)
            .join(Episode, VideoFile.episode_id == Episode.id)
            .join(Season, Episode.season_id == Season.id))).all()
    return music, manual, films, television


async def attach() -> int:
    """Point every picture the library holds a file for at that file."""
    music, manual, films, television = await _owned()

    def look():
        seen: dict = {}
        return (_music([tuple(r) for r in music], seen), _films([tuple(r) for r in films], seen),
                _television([tuple(r) for r in television], seen))

    (covers, portraits), (posters, backdrops), (shows, show_backdrops, seasons, stills) = \
        await asyncio.to_thread(look)
    slots = (
        (Release, Release.cover_url, {k: v for k, v in covers.items() if ("release", k) not in manual}),
        (Artist, Artist.image_url, {k: v for k, v in portraits.items() if ("artist", k) not in manual}),
        (Movie, Movie.poster_url, posters),
        (Movie, Movie.backdrop_url, backdrops),
        (Series, Series.poster_url, shows),
        (Series, Series.backdrop_url, show_backdrops),
        (Season, Season.poster_url, seasons),
        (Episode, Episode.still_url, stills),
    )
    attached = 0
    async with SessionLocal() as session:
        for model, column, found in slots:
            if not found:
                continue
            named = dict((await session.execute(
                select(model.id, column).where(model.id.in_(list(found))))).all())
            for entity_id, file in found.items():
                source = named.get(entity_id)
                if not source:
                    # nothing named a picture for it, so the file is the name
                    await session.execute(update(model).where(model.id == entity_id)
                                          .values({column.key: file}))
                    source = file
                if not store.drawable(source):
                    continue
                attached += await _attach(session, source, file)
        await session.commit()
    return attached


async def _attach(session, source: str, file: str) -> int:
    picture = (await session.execute(
        select(Picture).where(Picture.source == source))).scalar_one_or_none()
    if picture is None:
        await session.execute(insert(Picture).values(source=source, state=HELD, file=file)
                              .on_conflict_do_nothing(index_elements=[Picture.source]))
        return 1
    if picture.file == file and picture.state == HELD:
        return 0
    if picture.file and not picture.file.startswith("/"):
        (store.ROOT / picture.file).unlink(missing_ok=True)
    picture.state, picture.file, picture.tries, picture.retry_at = HELD, file, 0, None
    picture.kind = picture.width = picture.height = None
    store.nothing.discard(picture.id)
    return 1


async def _take(pictures: list[tuple[int, str]]) -> int:
    by_host: dict[str, list[int]] = defaultdict(list)
    for picture_id, source in pictures:
        by_host[urlsplit(source).hostname or ""].append(picture_id)

    async def one_host(ids: list[int]) -> int:
        taken = 0
        for picture_id in ids:
            if job.stopping:
                break
            try:
                picture = await store.hold(picture_id)
            except Exception:
                log.exception("picture %s could not be taken", picture_id)
                continue
            taken += picture is not None and picture.state == HELD
            job.state["taken"] += 1
        return taken

    return sum(await asyncio.gather(*(one_host(ids) for ids in by_host.values())))


async def fill() -> None:
    state = job.state
    async with SessionLocal() as session:
        registered = (await session.execute(REGISTER)).rowcount
        await session.commit()
    state["registered"] = registered
    state["phase"] = "attaching"
    state["attached"] = await attach()
    state["phase"] = "taking"
    after = 0
    while not job.stopping and (batch := await store.wanted(after, BATCH)):
        await _take(batch)
        after = batch[-1][0]
    log.info("pictures: %d registered, %d attached to files, %d asked for",
             state["registered"], state["attached"], state["taken"])


async def _round() -> None:
    state = await job.run(fill)
    if state["phase"] == "error":
        raise RuntimeError(state["error"])


async def pictures_loop():
    await schedule.every("pictures", INTERVAL, _round)
