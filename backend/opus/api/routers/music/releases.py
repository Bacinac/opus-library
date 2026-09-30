"""Release resource and everything hanging off it: the tracklist the album
page renders, queueing a grab, retagging linked files, and the destructive
side — deleting an album's files or a single stray one. A unit because these
all operate on one album's tracks and the files linked to them."""

import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from opus.music import lyrics
from opus.music.pipeline import choose, grab
from opus.api.routers.music.shared import _purge_release_files, _write_tag, edition_of
from opus.db import get_session
from opus.music.metadata import tracklists
from opus.models import Artist, MusicFile, Release, ReleaseStatus, Track
from opus.settings_store import current_runtime
from opus.music.tagging import tagger

router = APIRouter()
log = logging.getLogger("opus.api")


def _mbids(files) -> dict:
    """The MusicBrainz ids the file was tagged with, which is how a history
    kept elsewhere knows the recording rather than guessing it from names. A
    tag with several artists holds a list."""
    tags = (files[0].tags if files else None) or {}
    artists = tags.get("MUSICBRAINZ_ARTISTID") or []
    return {
        "recording": tags.get("MUSICBRAINZ_TRACKID"),
        "release": tags.get("MUSICBRAINZ_ALBUMID"),
        "artists": [artists] if isinstance(artists, str) else artists,
    }


@router.get("/tracks")
async def tracks_by_id(ids: str = "", session: AsyncSession = Depends(get_session)):
    """Several tracks, named by id, as the player draws them: whoever holds only
    ids gets one call rather than one per track, the same bargain the episodes
    endpoint strikes for a row of half-watched television."""
    wanted = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    if not wanted:
        return []
    rows = await session.execute(
        select(Track).where(Track.id.in_(wanted))
        .options(selectinload(Track.release).selectinload(Release.artist),
                 selectinload(Track.files))
    )
    return [
        {
            "id": t.id, "position": t.position, "title": t.title,
            "release_id": t.release_id, "album": t.release.title,
            "artist_id": t.release.artist_id,
            "artist": t.release.artist.name, "cover_url": t.release.cover_url,
            "duration_s": (t.files[0].duration_sec if t.files else None) or t.duration_sec,
            # a track whose file has gone is a track that cannot be carried on with
            "playable": bool(t.files),
            "mbids": _mbids(t.files),
        }
        for t in rows.scalars()
    ]


MOST_RECENT = 50


@router.get("/releases")
async def release_cards(ids: str = "", recent: int = 0, fresh: int = 0,
                        session: AsyncSession = Depends(get_session)):
    """Records as a shelf draws them: named by id for whoever keeps only ids,
    or the ones the house got last. Only records with something to play come
    back — a row of albums is a row of things to put on.

    `fresh` asks for what the followed artists put out in that many days, held
    or not: that list is news, and a record that is not here yet is the news
    worth having. A record announced for later is not out yet."""
    if fresh > 0:
        today = date.today()
        since = (today - timedelta(days=min(fresh, 365))).isoformat()
        wanted = list((await session.execute(
            select(Release.id)
            .join(Artist, Artist.id == Release.artist_id)
            .where(Artist.monitored.is_(True), Release.release_date >= since,
                   Release.release_date <= today.isoformat())
            .order_by(Release.release_date.desc())
            .limit(MOST_RECENT)
        )).scalars())
    elif recent > 0:
        newest = func.max(MusicFile.created_at)
        wanted = list((await session.execute(
            select(Track.release_id)
            .join(MusicFile, MusicFile.track_id == Track.id)
            .group_by(Track.release_id)
            .order_by(newest.desc())
            .limit(min(recent, MOST_RECENT))
        )).scalars())
    else:
        wanted = [int(i) for i in ids.split(",")[:MOST_RECENT] if i.strip().isdigit()]
    if not wanted:
        return []
    found = {r.id: r for r in (await session.execute(
        select(Release).where(Release.id.in_(wanted)).options(selectinload(Release.artist))
    )).scalars()}
    # counted in the database: a record's files carry their whole tag map, and
    # thirty records' worth of them took most of a second to load and drop
    tracks = dict((await session.execute(
        select(Track.release_id, func.count())
        .where(Track.release_id.in_(wanted)).group_by(Track.release_id)
    )).all())
    held_by = dict((await session.execute(
        select(Track.release_id, func.count(func.distinct(Track.id)))
        .join(MusicFile, MusicFile.track_id == Track.id)
        .where(Track.release_id.in_(wanted)).group_by(Track.release_id)
    )).all())
    cards = []
    for release_id in wanted:
        release = found.get(release_id)
        held = held_by.get(release_id, 0)
        if release and (held or fresh > 0):
            cards.append({
                "id": release.id, "title": release.title,
                "artist_id": release.artist_id, "artist": release.artist.name,
                "year": (release.release_date or "")[:4],
                "cover_url": release.cover_url, "held": held,
                "track_count": release.track_count or tracks.get(release_id, 0),
            })
    return cards


@router.get("/releases/{release_id}/tracks")
async def release_tracks(release_id: int, session: AsyncSession = Depends(get_session)):
    release = await session.get(Release, release_id)
    if release is None:
        raise HTTPException(404, "release not found")
    return await _tracklist(session, release)


@router.post("/releases/{release_id}/tracks")
async def fill_release_tracks(release_id: int, session: AsyncSession = Depends(get_session)):
    """Ask the sources for the tracklist and the article a release does not have
    yet — an unowned release has neither until somebody opens it — and answer
    with the tracklist as it then stands."""
    release = await session.get(Release, release_id)
    if release is None:
        raise HTTPException(404, "release not found")
    try:
        await tracklists.ensure_tracks(session, release)
    except tracklists.DiscographyError as exc:
        raise HTTPException(409, str(exc))
    try:
        await tracklists.ensure_description(release)
    except Exception:
        log.exception("album description fetch failed for release %s", release_id)
    await session.commit()
    return await _tracklist(session, release)


async def _tracklist(session, release: Release) -> dict:
    release_id = release.id
    result = await session.execute(
        select(Track)
        .where(Track.release_id == release_id)
        .order_by(Track.position)
        .options(selectinload(Track.files))
    )
    tracks = result.scalars().all()

    # every existing file must be visible somewhere: list the folder-mates
    # that matched no catalog track
    folders = {str(PurePosixPath(f.path).parent) for t in tracks for f in t.files}
    unmatched: dict[int, str] = {}
    for folder in folders:
        rows = await session.execute(
            select(MusicFile.id, MusicFile.path).where(
                MusicFile.track_id.is_(None),
                MusicFile.path.startswith(f"{folder}/", autoescape=True)
            )
        )
        for fid, p in rows:
            unmatched[fid] = PurePosixPath(p).name

    return {
        "description": release.description,
        "status": release.status,
        "links": [
            {"source": source, "url": url}
            for source, url in (
                ("deezer", f"https://www.deezer.com/album/{release.deezer_id}"
                 if release.deezer_id else None),
                ("discogs", f"https://www.discogs.com/master/{release.discogs_id}"
                 if release.discogs_id else None),
                ("spotify", f"https://open.spotify.com/album/{release.spotify_id}"
                 if release.spotify_id else None),
                ("wikidata", f"https://www.wikidata.org/wiki/{release.wikidata_id}"
                 if release.wikidata_id else None),
            ) if url
        ],
        "tracks": [
            {
                "id": t.id,
                "position": t.position,
                "title": t.title,
                "duration_sec": t.duration_sec,
                "has_file": bool(t.files),
                "file_id": t.files[0].id if t.files else None,
                "file_name": PurePosixPath(t.files[0].path).name if t.files else None,
                "tag_title": t.files[0].tag_title if t.files else None,
                "tag_artist": t.files[0].tag_artist if t.files else None,
                "tag_album": t.files[0].tag_album if t.files else None,
                "tag_track": t.files[0].tag_track if t.files else None,
                # everything the file carries, for the row that opens to show
                # it — NULL on a row probed before the column existed
                "tags": t.files[0].tags if t.files else None,
            }
            for t in tracks
        ],
        "unmatched_files": [
            {"id": fid, "name": name}
            for fid, name in sorted(unmatched.items(), key=lambda kv: kv[1])
        ],
    }


class TrackTitleBody(BaseModel):
    title: str


def _track_playback(track: Track, media: MusicFile, release: Release,
                    artist_name: str) -> dict:
    """One audio file, as something that can be handed to a player. Which file
    of however many editions the track holds is the caller's to have decided —
    this only describes the one it was given, codec included, so the surface
    that asked can tell a DSD file from a FLAC one without a second question."""
    return {
        "id": track.id, "position": track.position, "title": track.title,
        "artist": artist_name, "artist_id": release.artist_id,
        "album": release.title, "release_id": release.id,
        "cover_url": release.cover_url,
        "duration_s": media.duration_sec or track.duration_sec,
        "path": media.path, "codec": media.codec,
        "bitrate_kbps": media.bitrate_kbps, "sample_rate_hz": media.sample_rate_hz,
        "bit_depth": media.bit_depth, "channels": media.channels,
        "size": media.size,
    }


@router.get("/tracks/{track_id}/playback")
async def track_playback(track_id: int, prefer: str = "stereo",
                         session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Track).where(Track.id == track_id).options(
            selectinload(Track.files),
            selectinload(Track.release).selectinload(Release.artist)))
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(404, "track not found")
    media = _pick_edition(track, prefer)
    if media is None:
        raise HTTPException(409, "nothing has been imported for this track")
    return _track_playback(track, media, track.release, track.release.artist.name)


def _pick_edition(track: Track, prefer: str) -> MusicFile | None:
    """One file out of however many editions the track holds. Asked for what it
    cannot supply, a track hands over what it has — an album with only a stereo
    master is still an album to play."""
    files = sorted(track.files, key=lambda f: f.id)
    if not files:
        return None
    wanted = [f for f in files if edition_of(f) == prefer]
    return wanted[0] if wanted else files[0]


@router.get("/tracks/{track_id}/lyrics")
async def track_lyrics(track_id: int, refresh: bool = False,
                       session: AsyncSession = Depends(get_session)):
    """The words to a track, timed where anybody has timed them. Found on first
    ask and kept, so the second play of a record costs nothing; ?refresh=1 is
    for the one that came back wrong."""
    result = await session.execute(
        select(Track).where(Track.id == track_id).options(
            selectinload(Track.files),
            selectinload(Track.release).selectinload(Release.artist)))
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(404, "track not found")
    try:
        return await lyrics.words(session, track, refresh)
    except lyrics.Unreachable as exc:
        # said out loud: the screen offers to look again, where "this song has
        # no words" would have been the end of it
        raise HTTPException(502, str(exc))


@router.get("/releases/{release_id}/lyrics")
async def release_lyrics(release_id: int, session: AsyncSession = Depends(get_session)):
    """Which songs on this record have words — the mark beside a song, which is
    drawn before anybody presses it. One ask for the whole record; what it finds
    is written down, so putting the record on afterwards costs nothing."""
    result = await session.execute(
        select(Track).where(Track.release_id == release_id).options(
            selectinload(Track.files),
            selectinload(Track.release).selectinload(Release.artist)))
    tracks = list(result.scalars())
    if not tracks:
        raise HTTPException(404, "release not found")
    return await lyrics.album(session, tracks)


@router.get("/releases/{release_id}/about")
async def release_about(release_id: int, session: AsyncSession = Depends(get_session)):
    """What the album is, in a paragraph — asked for AFTER the music started, by
    a screen that has room to say it. Read as the records pass wrote it down: a
    listener's screen does not send the library off to Wikipedia."""
    release = await session.get(Release, release_id)
    if release is None:
        raise HTTPException(404, "release not found")
    return {"id": release.id, "title": release.title,
            "release_date": release.release_date,
            "description": release.description or "",
            "label": release.label or "",
            "genres": release.genres or []}


@router.get("/releases/{release_id}/playback")
async def release_playback(release_id: int, prefer: str = "stereo",
                           session: AsyncSession = Depends(get_session)):
    """An album as a queue: the tracks that have a file, in order.

    A record can be held in more than one edition — the stereo master and the
    surround mix are different records of the same music, not better and worse
    copies — so the caller says which it can play. What is on offer comes back
    with it, because the screen that asked has no other way to know."""
    result = await session.execute(
        select(Release).where(Release.id == release_id).options(
            selectinload(Release.artist),
            selectinload(Release.tracks).selectinload(Track.files)))
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(404, "release not found")
    artist_name = release.artist.name
    chosen = {t.id: _pick_edition(t, prefer) for t in release.tracks}
    return {
        "id": release.id, "title": release.title,
        "artist": artist_name, "artist_id": release.artist_id,
        "cover_url": release.cover_url, "release_date": release.release_date,
        # every edition this album is held in, so the surface can offer them
        "editions": sorted({edition_of(f) for t in release.tracks for f in t.files}),
        "tracks": [
            _track_playback(t, chosen[t.id], release, artist_name)
            for t in sorted(release.tracks, key=lambda t: t.position)
            if chosen.get(t.id)
        ],
    }


@router.patch("/tracks/{track_id}")
async def update_track_title(track_id: int, body: TrackTitleBody,
                             session: AsyncSession = Depends(get_session)):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "title must not be empty")
    if len(title) > 500:
        raise HTTPException(400, "title too long")
    track = await session.get(Track, track_id)
    if track is None:
        raise HTTPException(404, "track not found")
    track.title = title
    track.title_manual = True
    await session.commit()
    return {"ok": True, "title": title}


@router.post("/releases/{release_id}/retag")
async def retag_release(release_id: int, session: AsyncSession = Depends(get_session)):
    """Album-level retag: align title, numbering, album/artist and date tags
    of every linked file with the catalog."""
    result = await session.execute(
        select(Release)
        .where(Release.id == release_id)
        .options(selectinload(Release.tracks).selectinload(Track.files),
                 selectinload(Release.artist))
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(404, "release not found")
    tracks = sorted(release.tracks, key=lambda t: t.position)
    if not any(t.files for t in tracks):
        raise HTTPException(409, "no files linked to this release")
    year = release.release_date[:4] if release.release_date else None
    total = release.track_count or len(tracks)
    artist_name = release.artist.name
    files_done = 0
    for track in tracks:
        for file in track.files:
            await _write_tag(
                tagger.retag_file, Path(file.path), artist_name,
                release.title, track.title, track.position, total, year,
            )
            file.tag_artist = artist_name
            file.tag_album = release.title
            file.tag_title = track.title
            file.tag_track = track.position
            files_done += 1
    await session.commit()
    return {"status": "ok", "files": files_done}


@router.post("/tracks/{track_id}/fix-tag")
async def fix_track_tag(track_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Track).where(Track.id == track_id).options(selectinload(Track.files))
    )
    track = result.scalar_one_or_none()
    if track is None:
        raise HTTPException(404, "track not found")
    if not track.files:
        raise HTTPException(409, "no file linked to this track")
    for file in track.files:
        await _write_tag(tagger.write_title_tag, Path(file.path), track.title)
        file.tag_title = track.title
    await session.commit()
    return {"status": "ok", "files": len(track.files)}


@router.delete("/files/{file_id}")
async def delete_file(file_id: int,
                      session: AsyncSession = Depends(get_session)):
    """Delete a single audio file from disk (and its row) — for a stray or
    mis-tagged folder-mate. Never touches anything outside the library root."""
    file = await session.get(MusicFile, file_id)
    if file is None:
        raise HTTPException(404, "file not found")
    config = await current_runtime()
    music_root = Path(config.get("music_dir")).resolve()
    path = Path(file.path)
    if music_root not in path.resolve().parents:
        raise HTTPException(400, "file is outside the library root")
    try:
        if path.exists():
            await asyncio.to_thread(path.unlink)
    except OSError as exc:
        raise HTTPException(500, f"failed to delete {path.name}: {exc}")
    await session.delete(file)
    await session.commit()
    return {"ok": True, "name": path.name}


@router.delete("/releases/{release_id}/files")
async def delete_release_files(release_id: int,
                               session: AsyncSession = Depends(get_session)):
    """Delete the album's audio files from disk (and their rows) — the catalog
    entry stays, so the album drops back to a downloadable state."""
    result = await session.execute(
        select(Release).where(Release.id == release_id)
        .options(selectinload(Release.tracks).selectinload(Track.files))
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(404, "release not found")
    config = await current_runtime()
    music_root = Path(config.get("music_dir")).resolve()
    if not any(t.files for t in release.tracks):
        raise HTTPException(409, "no files linked to this release")
    removed = await _purge_release_files(session, release, music_root)
    release.status = ReleaseStatus.NONE
    await session.commit()
    return {"ok": True, "removed": removed}


class DownloadBody(BaseModel):
    mode: str = "full"  # full | replace | surround | dsd


@router.post("/releases/{release_id}/download", status_code=202)
async def download_release(release_id: int, body: DownloadBody | None = None,
                           session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Release).where(Release.id == release_id).options(selectinload(Release.tracks))
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(404, "release not found")
    if release.status in (ReleaseStatus.SEARCHING, ReleaseStatus.DOWNLOADING):
        raise HTTPException(409, f"release is already {release.status}")
    asked = body.mode if body else "full"
    if asked in ("surround", "dsd") and not release.tracks:
        raise HTTPException(409, "the other edition of an album nobody holds yet")

    if not release.tracks:
        try:
            await tracklists.ensure_tracks(session, release)
        except tracklists.DiscographyError as exc:
            raise HTTPException(409, str(exc))

    release.status = ReleaseStatus.WANTED
    await session.commit()

    # An album is acquired whole, from one source. 'replace' is the same search
    # that discards anything failing to deliver every track; 'surround' and
    # 'dsd' each look for the OTHER edition of a record already held and file
    # it beside the first rather than over it.
    mode = asked if asked in ("full", "replace", "surround", "dsd") else "full"
    grab.spawn_grab(release_id, mode)
    return {"status": "queued"}


@router.get("/releases/{release_id}/candidates")
async def release_candidates(release_id: int, session: AsyncSession = Depends(get_session)):
    """Every candidate any enabled channel finds for this album, scored but
    unfiltered — nothing dropped for a zero match, a ceiling, or a missing
    format word. For a human to choose from once the automatic grab already
    tried this release and nothing on its own was good enough to take: the
    DSD edition of a record the automatic matcher would never guess is one, an
    audiophile label's own post whose title names no artist the catalog
    knows, the one candidate the ceiling excluded by a single kHz."""
    release = await session.get(Release, release_id)
    if release is None:
        raise HTTPException(404, "release not found")
    scored = await choose.search_candidates(release_id)
    if scored is None:
        raise HTTPException(404, "release not found")
    return {"candidates": [
        {"channel": s.candidate.channel, "title": s.candidate.title, "ref": s.candidate.ref,
         "score": round(s.score, 1), "completeness": round(s.completeness, 2),
         "multi_album": s.multi_album, "whole_album": s.candidate.whole_album,
         "size": sum(f.size for f in s.candidate.files) or None,
         "quality": {"codec": s.quality.codec, "bit_depth": s.quality.bit_depth,
                     "sample_rate_khz": s.quality.sample_rate_khz,
                     "bitrate_kbps": s.quality.bitrate_kbps, "dsd": s.quality.dsd,
                     "lossless": s.quality.lossless}}
        for s in scored
    ]}


class GrabBody(BaseModel):
    channel: str
    title: str
    ref: dict
    mode: str = "full"  # full | replace | surround | dsd
    multi_album: bool = False


@router.post("/releases/{release_id}/grab", status_code=202)
async def grab_candidate(release_id: int, body: GrabBody,
                         session: AsyncSession = Depends(get_session)):
    """Download exactly the candidate a human picked from the list above — no
    automatic matching, no manifest re-check, no title-completeness gate. The
    one entry point that can acquire an album the catalog cannot name a track
    of, or take the DSD edition of one it can."""
    result = await session.execute(
        select(Release).where(Release.id == release_id).options(selectinload(Release.tracks))
    )
    release = result.scalar_one_or_none()
    if release is None:
        raise HTTPException(404, "release not found")
    if release.status in (ReleaseStatus.SEARCHING, ReleaseStatus.DOWNLOADING):
        raise HTTPException(409, f"release is already {release.status}")
    if body.mode in ("surround", "dsd") and not release.tracks:
        raise HTTPException(409, "the other edition of an album nobody holds yet")
    ok = await grab.grab_candidate(release_id, body.channel, body.title, body.ref,
                                       body.mode, body.multi_album)
    if not ok:
        raise HTTPException(409, f"{body.channel} refused {body.title!r}")
    return {"status": "queued"}
