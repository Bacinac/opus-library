"""Library page endpoints: the two long-running background passes (scan and
enrich) with the progress state the UI polls, the unmatched-folder list they
leave behind, and the manual repairs on it — rematch, delete, bulk redownload.
A unit because they all act on the library as a whole rather than on one
catalog row."""

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy import and_, any_, bindparam, delete as sql_delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from opus.music.pipeline import grab
from opus.api.routers.music.shared import _prune_empty_dirs, _purge_release_files
from opus.db import get_session
from opus.music.library import enrich_all, retitle, scan
from opus.models import Artist, MusicFile, Release, ReleaseStatus, Track
from opus.music.metadata.catalog import CatalogClient
from opus.music.textnorm import answers, artist_key, norm
from opus.settings_store import current_runtime

router = APIRouter()
log = logging.getLogger(__name__)


@router.post("/library/incomplete/{release_id}/redownload", status_code=202)
async def redownload_album(release_id: int, session: AsyncSession = Depends(get_session)):
    """Wipe one doubtful album and acquire it again from scratch.

    Deleting first is the point — no album is preferable to half of one, or to
    one assembled from two rips — so a grab that finds nothing leaves the album
    empty, deliberately.

    One album, because "doubtful" is two faults under one name and some of what
    it counts is no fault at all: a catalogue offering a box set's track list
    against a single record, or a single filed as an album. Those cannot be
    fixed by fetching, and the same sentence that empties one album on purpose
    would throw away eighty good tracks for the sake of one that is missing.
    Which album this is worth doing to is a judgement made in front of the
    album, so it is asked for one at a time.
    """
    config = await current_runtime()
    music_root = Path(config.get("music_dir")).resolve()
    release = (await session.execute(
        select(Release)
        .options(selectinload(Release.tracks).selectinload(Track.files))
        .where(Release.id == release_id))).scalar_one_or_none()
    if release is None:
        raise HTTPException(404, "no such album")
    if release.status in (ReleaseStatus.SEARCHING, ReleaseStatus.DOWNLOADING):
        raise HTTPException(409, "this album is already being fetched")
    await _purge_release_files(session, release, music_root)
    release.status = ReleaseStatus.WANTED
    await session.commit()
    grab.spawn_grab_bulk([release_id])
    return {"release": release_id}


@router.post("/library/scan", status_code=202)
async def start_library_scan():
    if not scan.start():
        raise HTTPException(409, "scan already running")
    return {"status": "started"}


@router.get("/library/scan")
async def library_scan_status():
    return scan.job.state


@router.get("/library/incomplete")
async def incomplete_albums(session: AsyncSession = Depends(get_session)):
    """The albums the stats call doubtful, named.

    The count on the shelf says how many; this says which, whose they are and
    how much of each is missing. That is what makes the number actionable: a
    doubtful album is answered in front of the album, and a third of this list
    turns out to want nothing done to it at all."""
    per_album = (
        select(
            Track.release_id.label("release_id"),
            func.count(MusicFile.id).label("files"),
            func.count().filter(MusicFile.id.is_(None)).label("unfilled"),
            func.count(func.distinct(MusicFile.download_id)).label("origins"),
        )
        .select_from(Track)
        .outerjoin(MusicFile, MusicFile.track_id == Track.id)
        .group_by(Track.release_id)
        .subquery()
    )
    rows = (await session.execute(
        select(Release.id, Release.title, Release.release_date,
               Artist.id, Artist.name,
               per_album.c.files, per_album.c.unfilled, per_album.c.origins)
        .select_from(per_album)
        .join(Release, Release.id == per_album.c.release_id)
        .join(Artist, Artist.id == Release.artist_id)
        .where(per_album.c.files > 0,
               or_(per_album.c.unfilled > 0, per_album.c.origins > 1))
        .order_by(Artist.name, Release.release_date)
    )).all()
    return [
        {
            "release_id": rid,
            "title": title,
            "year": (date or "")[:4] or None,
            "artist_id": aid,
            "artist": artist,
            "have": files,
            "need": files + unfilled,
            "origins": origins,
        }
        for rid, title, date, aid, artist, files, unfilled, origins in rows
    ]


@router.get("/library/stats")
async def library_stats(session: AsyncSession = Depends(get_session)):
    """Whole-library totals: monitored artists, albums actually held, tracks on
    disk, and the same complete/doubtful split the incomplete list is drawn from —
    an album holding only part of its tracks, or stitched from more than one
    download, counts as doubtful.

    One pass over tracks and their files, grouped by album. Asking the same
    three questions as correlated subqueries per album is the same answer and
    took 70 seconds over 48k files: the page waited a minute for five numbers."""
    per_album = (
        select(
            Track.release_id.label("release_id"),
            func.count(MusicFile.id).label("files"),
            func.count().filter(MusicFile.id.is_(None)).label("unfilled"),
            func.count(func.distinct(MusicFile.download_id)).label("origins"),
        )
        .select_from(Track)
        .outerjoin(MusicFile, MusicFile.track_id == Track.id)
        .group_by(Track.release_id)
        .subquery()
    )
    held_col = per_album.c.files > 0
    doubtful_col = and_(held_col, or_(per_album.c.unfilled > 0, per_album.c.origins > 1))
    counts = (await session.execute(
        select(
            func.count().filter(held_col),
            func.count().filter(doubtful_col),
        ).select_from(per_album)
    )).one()
    held, doubtful = counts

    artists = await session.scalar(
        select(func.count()).select_from(Artist).where(Artist.monitored))
    tracks = await session.scalar(select(func.count()).select_from(MusicFile))
    # The years the shelf runs between, read off the records that are ON it and
    # not off the artists' begin_years: the year a band was formed is not a year
    # of music.
    span = (await session.execute(
        select(func.min(func.left(Release.release_date, 4)),
               func.max(func.left(Release.release_date, 4)))
        .select_from(Release)
        .join(per_album, per_album.c.release_id == Release.id)
        .where(held_col, Release.release_date.isnot(None), Release.release_date != "")
    )).one()
    years = [int(y) for y in span if y and y.isdigit()]
    return {
        "artists": artists or 0,
        "albums": held or 0,
        "tracks": tracks or 0,
        "complete": (held or 0) - (doubtful or 0),
        "doubtful": doubtful or 0,
        "span": [min(years), max(years)] if len(years) == 2 and years[0] != years[1] else None,
    }


@router.get("/library/unmatched")
async def unmatched_folders(session: AsyncSession = Depends(get_session)):
    """Folders none of whose files link to any catalog track — compilations
    and albums no source could identify. Derived from the files table, so it
    survives restarts (scan results are in-memory only)."""
    config = await current_runtime()
    root = str(config.get("music_dir") or "").rstrip("/") + "/"
    folder = func.regexp_replace(MusicFile.path, "/[^/]+$", "").label("folder")
    rows = (await session.execute(
        select(
            folder,
            func.count().label("files"),
            func.max(MusicFile.tag_artist).label("tag_artist"),
            func.max(MusicFile.tag_album).label("tag_album"),
        )
        .group_by(folder)
        .having(func.count(MusicFile.track_id) == 0)
        .order_by(folder)
    )).all()
    return [
        {
            "folder": r.folder[len(root):] if r.folder.startswith(root) else r.folder,
            "files": r.files,
            "tag_artist": r.tag_artist,
            "tag_album": r.tag_album,
        }
        for r in rows
    ]


class FolderBody(BaseModel):
    folder: str


@router.post("/library/folders/rematch")
async def rematch_library_folder(body: FolderBody):
    if scan.job.running:
        raise HTTPException(409, "library scan is running")
    try:
        return await scan.rematch_folder(body.folder)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


@router.delete("/library/folders")
async def delete_library_folder(body: FolderBody,
                                session: AsyncSession = Depends(get_session)):
    """Delete an entire unmatched folder from disk (and its file rows).
    Refuses folders holding catalog-linked files; never touches anything
    outside the library root."""
    config = await current_runtime()
    music_root = Path(config.get("music_dir")).resolve()
    target = (music_root / body.folder).resolve()
    if music_root not in target.parents:
        raise HTTPException(400, "folder is outside the library root")
    if not target.is_dir():
        raise HTTPException(404, "folder not found")
    linked = (await session.execute(
        select(func.count()).select_from(MusicFile)
        .where(MusicFile.path.startswith(f"{target}/", autoescape=True),
               MusicFile.track_id.is_not(None))
    )).scalar()
    if linked:
        raise HTTPException(409, "folder holds files linked to the catalog")
    try:
        await asyncio.to_thread(shutil.rmtree, target)
    except OSError as exc:
        raise HTTPException(500, f"failed to delete folder: {exc}")
    # prune now-empty parents (an artist dir left behind), never the root
    await _prune_empty_dirs(target.parent, music_root)
    await session.execute(
        sql_delete(MusicFile)
        .where(MusicFile.path.startswith(f"{target}/", autoescape=True))
    )
    await session.commit()
    return {"ok": True}


@router.post("/library/enrich", status_code=202)
async def start_library_enrich(rescan_after: bool = False, force: bool = False):
    if not enrich_all.start(rescan_after=rescan_after, force=force):
        raise HTTPException(409, "enrich already running")
    return {"status": "started"}


@router.get("/library/enrich")
async def library_enrich_status():
    return enrich_all.job.state


@router.get("/search")
async def search(q: str, session: AsyncSession = Depends(get_session)):
    """Everything a few words find in music, for a player's one search box: the
    artists and the songs on the shelf, then the records a catalogue has that
    the shelf does not. Records rather than artists, because a record is what
    gets fetched; and only hits whose names answer the words."""
    words = q.strip()
    if not words:
        return {"artists": [], "tracks": [], "records": []}

    async def catalogue() -> list[dict]:
        catalog = CatalogClient()
        try:
            return await catalog.search_albums(words)
        except Exception as exc:
            log.warning("music search: the catalogue did not answer %r: %s", words, exc)
            return []
        finally:
            await catalog.close()

    async def on_the_shelf():
        shelf = (await session.execute(
            select(Artist.id, Artist.name).where(Artist.monitored).order_by(Artist.name))).all()
        artists = [artist_id for artist_id, name in shelf if answers(words, name)]
        tracks = await search_library(words, 30, session)
        held = (await session.execute(
            select(Artist.name, Release.title).join(Release.artist)
            .where(Release.tracks.any(Track.files.any())))).all()
        return artists, tracks, held

    found, (artists, tracks, held) = await asyncio.gather(catalogue(), on_the_shelf())
    held = {(artist_key(name), " ".join(norm(title).split())) for name, title in held}
    records, seen = [], set()
    for album in found:
        artist = (album.get("artist") or {}).get("name") or ""
        title = album.get("title") or ""
        key = (artist_key(artist), " ".join(norm(title).split()))
        if key in held or key in seen or not (
                answers(words, artist) or answers(words, title) or answers(words, f"{artist} {title}")):
            continue
        seen.add(key)
        released = album.get("release_date") or ""
        records.append({
            "kind": "album", "source": album.get("source"), "id": str(album["id"]),
            "title": title, "artist": artist,
            "year": int(released[:4]) if released[:4].isdigit() else None,
            "cover_url": album.get("cover_medium") or album.get("cover_xl"),
        })
    return {"artists": artists, "tracks": tracks, "records": records}


@router.get("/search/library")
async def search_library(q: str, limit: int = 50,
                         session: AsyncSession = Depends(get_session)):
    """Songs on the shelf whose title, record or artist says the words.

    The other search asks Deezer what exists; this one asks the shelf what is
    here, which is the question a voice in a car is asking — "play Hotel
    California" wants the file, not the discography. Only songs with a file,
    because a hit that cannot be played is not an answer."""
    words = q.strip()
    if not words:
        return []
    # "kazaliste" is how Prljavo Kazalište is typed on a phone
    # written into the statement rather than bound: once prepared, a generic
    # plan reads the trigram index for "a" too, which is the whole index
    like = func.unaccented(bindparam("words", f"%{words}%", literal_execute=True))
    # each table asked once, through its own trigram index; an OR across the
    # join unaccented every record and artist name once for each of its tracks
    artists = select(Artist.id).where(func.unaccented(Artist.name).ilike(like))
    records = select(Release.id).where(or_(
        func.unaccented(Release.title).ilike(like),
        Release.artist_id == any_(func.array(artists.scalar_subquery()))))
    rows = await session.execute(
        select(Track)
        .join(Track.release).join(Release.artist)
        .where(Track.files.any(),
               or_(func.unaccented(Track.title).ilike(like),
                   Track.release_id == any_(func.array(records.scalar_subquery()))))
        .options(selectinload(Track.release).selectinload(Release.artist),
                 selectinload(Track.files).load_only(MusicFile.duration_sec, MusicFile.codec))
        .order_by(Artist.name, Release.title, Track.position, Track.id)
        .limit(min(limit, 200)))
    return [
        {
            "id": t.id, "position": t.position, "title": t.title,
            "release_id": t.release_id, "album": t.release.title,
            "artist": t.release.artist.name, "artist_id": t.release.artist_id,
            "cover_url": t.release.cover_url,
            "duration_s": t.files[0].duration_sec or t.duration_sec,
            "codec": t.files[0].codec,
        }
        for t in rows.scalars()
    ]


@router.get("/library/tags")
async def library_tags(q: str = "", limit: int = 100, offset: int = 0,
                       session: AsyncSession = Depends(get_session)):
    """Every song and everything its file says, in one table.

    The tags were readable a row at a time on the page about one record, which
    answers a question about that record and not the one somebody actually has:
    what is in my files, and where does it disagree with itself."""
    keys = (await session.execute(sa.text(
        "SELECT key, count(*) AS files FROM music_files f,"
        " jsonb_object_keys(f.tags) AS key WHERE f.tags IS NOT NULL"
        " GROUP BY key ORDER BY 2 DESC, 1"))).all()

    rows = select(MusicFile).where(MusicFile.tags.is_not(None))
    if q:
        like = f"%{q}%"
        rows = rows.where(or_(MusicFile.tag_artist.ilike(like),
                                 MusicFile.tag_album.ilike(like),
                                 MusicFile.tag_title.ilike(like),
                                 MusicFile.path.ilike(like)))
    total = (await session.execute(
        select(func.count()).select_from(rows.subquery()))).scalar_one()
    found = (await session.execute(
        rows.order_by(MusicFile.tag_artist, MusicFile.tag_album,
                      MusicFile.tag_track, MusicFile.path)
        .limit(min(limit, 500)).offset(offset))).scalars().all()

    return {
        "total": total,
        "keys": [{"key": key, "files": files} for key, files in keys],
        "rows": [
            {"id": f.id, "path": f.path, "artist": f.tag_artist,
             "album": f.tag_album, "title": f.tag_title, "track": f.tag_track,
             "tags": f.tags or {}}
            for f in found
        ],
    }


@router.post("/library/retitle", status_code=202)
async def start_library_retitle(write_tags: bool = True, artist_id: int | None = None):
    """Put every title into the one form, and the files with it.

    `write_tags=false` moves the catalogue and leaves the files alone, counting
    what it would have written — how to see the pass before it touches a disk.
    `artist_id` confines it to one artist, which is how a first run is taken."""
    if not retitle.job.start(retitle.retitle, write_tags, artist_id):
        raise HTTPException(409, "retitle already running")
    return {"status": "started"}


@router.get("/library/retitle")
async def library_retitle_status():
    return retitle.job.state
