"""Series down to the episode: the tree the library holds, and searching or
grabbing at whichever level the user is standing on — one episode, or a whole
season at once.

A season grab runs in the background and skips what has not aired: a series you
follow is not a standing order for every episode of it."""

import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException
import sqlalchemy as sa
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from opus.acquire import AcquireError
from opus.api.routers.video.shared import (
    AddByTmdbId,
    ItemPatch,
    ReleaseSelection,
    SeasonPatch,
    _candidate_from_selection,
    _playback_json,
    _resolution,
    held_langs,
    overview_in,
)
from opus import passes
from opus.db import SessionLocal, get_session
from opus.models import Episode, Season, Series, Subtitle, VideoDownload, VideoFile
from opus.settings_store import current_runtime
from opus.video.pipeline import grab, items, score, search
from opus.video.metadata import tmdb
from opus.video.subtitles.policy import effective_policy, evaluate, policy_met

log = logging.getLogger(__name__)
router = APIRouter()


def _episode_json(config, episode: Episode, active: set[int], lang: str = "en") -> dict:
    present: list[str] = []
    file_info = None
    if episode.files:
        subs = [s for f in episode.files for s in f.subtitles]
        policy = effective_policy(config, episode.season.series.subtitle_override)
        result = evaluate(policy, subs, accept_auto=config.bool("accept_auto_subs"))
        status = "complete" if result.satisfied else "waiting_subtitles"
        missing = list(result.missing)
        present = list(result.present)
        media = episode.files[0]
        file_info = {"resolution": _resolution(media), "video_codec": media.video_codec,
                     "size": media.size}
    elif episode.id in active:
        status, missing = "downloading", []
    elif not items.wanted(episode):
        # the same question the pass asks before it chases anything, asked of
        # the same function: a word that says "wanted" while nothing is looking
        # for it, or "ignored" while something is, is worse than no word
        status, missing = "ignored", []
    else:
        status, missing = "wanted", []
    return {"id": episode.id, "number": episode.number, "title": episode.title,
            # what the episode is about
            "overview": overview_in(episode, lang),
            "air_date": episode.air_date.isoformat() if episode.air_date else None,
            "still_url": episode.still_url, "runtime_min": episode.runtime_min,
            "monitored": episode.monitored, "status": status,
            # a copy on the shelf and a better one on its way
            "replacing": bool(episode.files) and episode.id in active,
            "missing_subs": missing, "present_subs": present, "file": file_info}


@router.get("/series")
async def series_list(lang: str = "en", session=Depends(get_session)):
    """The shelf of series, counted in the database rather than in Python.

    Loading the tree — every series, its seasons, their episodes, those episodes'
    files and each file's subtitles — builds tens of thousands of objects to
    produce two numbers per row, and took the better part of a second for
    fifty-nine series. The counting happens in SQL; only the subtitle languages
    come back, because whether a policy is satisfied is a decision with
    per-series overrides and belongs in the code that owns it."""
    config = await current_runtime()

    rows = list((await session.execute(
        select(Series).order_by(Series.added_at.desc())
    )).scalars())

    # how many episodes each series has, and how many of them are on disk
    counted = (await session.execute(
        select(Season.series_id,
               func.count(Episode.id.distinct()),
               func.count(VideoFile.episode_id.distinct()))
        .select_from(Season)
        .join(Episode, Episode.season_id == Season.id)
        .outerjoin(VideoFile, VideoFile.episode_id == Episode.id)
        .group_by(Season.series_id)
    )).all()
    totals = {sid: (total, owned) for sid, total, owned in counted}

    langs: dict[int, list] = {}
    for series_id, found in (await session.execute(
        select(Season.series_id, held_langs(config.bool("accept_auto_subs")))
        .select_from(Season)
        .join(Episode, Episode.season_id == Season.id)
        .join(VideoFile, VideoFile.episode_id == Episode.id)
        .outerjoin(Subtitle, Subtitle.file_id == VideoFile.id)
        .group_by(Season.series_id, Episode.id)
    )).all():
        langs.setdefault(series_id, []).append(found)

    out = []
    for series in rows:
        total, owned = totals.get(series.id, (0, 0))
        complete = sum(policy_met(config, series.subtitle_override, found)
                       for found in langs.get(series.id, []))
        out.append({
            "id": series.id, "tmdb_id": series.tmdb_id, "title": series.title,
            "year": series.year, "poster_url": series.poster_url,
            "backdrop_url": series.backdrop_url,
            "overview": overview_in(series, lang), "genres": series.genres,
            "studios": series.studios, "directors": series.directors,
            "status": series.status, "monitored": series.monitored,
            # denominator = episodes you actually have (adopted); an empty
            # monitored series falls back to the full TMDB count (to grab)
            "episodes_total": owned if owned else total,
            "episodes_complete": complete,
            # the other question, which the pair above cannot answer: how much of
            # the series is here. A run being fetched is 84 of 179, and saying
            # 84/84 because both counts were drawn from what is on disk tells
            # somebody watching it arrive that it has arrived.
            "episodes_have": owned,
            "episodes_known": total,
        })
    return out


@router.get("/series/{series_id}")
async def series_detail(series_id: int, lang: str = "en", session=Depends(get_session)):
    config = await current_runtime()
    result = await session.execute(
        select(Series).where(Series.id == series_id).options(
            selectinload(Series.seasons).selectinload(Season.episodes)
            .selectinload(Episode.files).selectinload(VideoFile.subtitles)))
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404)
    active = await items.active_ids(session, VideoDownload.episode_id)
    return {
        "id": series.id, "tmdb_id": series.tmdb_id, "title": series.title,
        "year": series.year, "overview": overview_in(series, lang),
        "poster_url": series.poster_url, "backdrop_url": series.backdrop_url,
        "status": series.status,
        "genres": series.genres, "studios": series.studios,
        "directors": series.directors, "cast": series.cast,
        "monitored": series.monitored, "subtitle_override": series.subtitle_override,
        "seasons": [{
            "number": season.number, "monitored": season.monitored,
            "overview": overview_in(season, lang), "poster": season.poster_url or None,
            "episodes": [_episode_json(config, e, active, lang) for e in season.episodes],
        } for season in series.seasons],
    }


@router.post("/series", status_code=201)
async def series_add(body: AddByTmdbId, session=Depends(get_session)):
    config = await current_runtime()
    existing = await session.execute(select(Series).where(Series.tmdb_id == body.tmdb_id))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="already added")
    try:
        details = await tmdb.series_details(config, body.tmdb_id)
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    seasons = details.pop("seasons")
    series = Series(**details)
    session.add(series)
    await session.flush()
    episode_count = 0
    for s in seasons:
        season = Season(series_id=series.id, number=s["number"],
                        overview=s["overview"], overview_hr=s["overview_hr"],
                        poster_url=s["poster_url"])
        session.add(season)
        await session.flush()
        for e in s["episodes"]:
            session.add(Episode(season_id=season.id, tmdb_id=e["tmdb_id"],
                                number=e["number"], title=e["title"],
                                air_date=e["air_date"], overview=e["overview"],
                                overview_hr=e.get("overview_hr", ""),
                                still_url=e.get("still_url"), runtime_min=e.get("runtime_min")))
            episode_count += 1
    await session.commit()
    return {"id": series.id, "title": series.title, "episodes": episode_count}


@router.patch("/series/{series_id}")
async def series_patch(series_id: int, body: ItemPatch, session=Depends(get_session)):
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(status_code=404)
    if body.monitored is not None:
        series.monitored = body.monitored
    if body.subtitle_override is not None:
        series.subtitle_override = body.subtitle_override
    await session.commit()
    return {"ok": True}


@router.delete("/series/{series_id}", status_code=204)
async def series_delete(series_id: int, session=Depends(get_session)):
    await session.execute(delete(Series).where(Series.id == series_id))
    await session.commit()


@router.get("/episodes")
async def episodes_by_id(ids: str = "", lang: str = "en", session=Depends(get_session)):
    """A handful of episodes by id, for a consumer that holds references and no
    catalogue of its own: showing eight half-watched episodes should cost one
    call, not eight. Unknown ids are left out rather than raising — a reference
    to something since deleted is a stale bookmark, not an error."""
    wanted = {int(x) for x in ids.split(",") if x.strip().lstrip("-").isdigit()}
    if not wanted:
        return []
    result = await session.execute(
        select(Episode).where(Episode.id.in_(wanted)).options(
            selectinload(Episode.season).selectinload(Season.series),
            selectinload(Episode.files)))
    out = []
    for episode in result.scalars():
        series = episode.season.series
        out.append({
            "id": episode.id, "number": episode.number, "title": episode.title,
            "season_number": episode.season.number,
            "series_id": series.id, "series_title": series.title,
            "series_tmdb_id": series.tmdb_id,
            "poster_url": series.poster_url, "backdrop_url": series.backdrop_url,
            # the episode's own story, and the series' when TMDB wrote none
            "overview": overview_in(episode, lang) or overview_in(series, lang),
            "year": series.year, "genres": series.genres,
            "playable": bool(episode.files),
            "duration_s": episode.files[0].duration_s if episode.files else None,
        })
    return out


@router.get("/episodes/{episode_id}/playback")
async def episode_playback(episode_id: int, lang: str = "en", session=Depends(get_session)):
    """What /movies/{id}/playback answers, for one episode. The title carries
    the series and the number because an episode played on its own is otherwise
    a name with no place: "Chapter Six" says nothing on a transport bar."""
    result = await session.execute(
        select(Episode).where(Episode.id == episode_id).options(
            selectinload(Episode.season).selectinload(Season.series),
            selectinload(Episode.files).selectinload(VideoFile.subtitles),
            selectinload(Episode.files).selectinload(VideoFile.streams),
                 selectinload(Episode.files).selectinload(VideoFile.chapters)))
    episode = result.scalar_one_or_none()
    if episode is None:
        raise HTTPException(status_code=404)
    if not episode.files:
        raise HTTPException(status_code=409,
                            detail="nothing has been imported for this episode")
    series = episode.season.series
    number = f"S{episode.season.number:02d}E{episode.number:02d}"
    running = (await session.execute(
        select(Season.number, Episode.number, Episode.id, Episode.title,
               sa.exists().where(VideoFile.episode_id == Episode.id))
        .join(Episode.season).where(Season.series_id == series.id, Season.number > 0)
        .order_by(Season.number, Episode.number))).all()
    here = (episode.season.number, episode.number)
    later = [row for row in running if (row[0], row[1]) > here]
    # the one after it in the running order, if it is on the disk. Specials sit
    # outside that order, so an episode of season 0 leads nowhere
    after = later[0] if later and episode.season.number > 0 else None
    return {
        "title": f"{series.title} · {number}"
                 + (f" · {episode.title}" if episode.title else ""),
        "season_left": sum(1 for row in later if row[0] == episode.season.number),
        "next": {"id": after[2], "season_number": after[0], "number": after[1],
                 "title": after[3]} if after and after[4] else None,
        "year": series.year,
        "runtime_min": None,
        "backdrop_url": series.backdrop_url,
        # the episode tells its own story; who is in it is the series' answer
        "details": {"overview": overview_in(episode, lang) or overview_in(series, lang),
                    "genres": series.genres, "directors": series.directors,
                    "cast": series.cast},
        "series_id": series.id,
        "season_number": episode.season.number,
        "episode_number": episode.number,
        **_playback_json(episode.files[0]),
    }


@router.post("/episodes/{episode_id}/search", status_code=202)
async def episode_search(episode_id: int, session=Depends(get_session)):
    config = await current_runtime()
    result = await session.execute(
        select(Episode).where(Episode.id == episode_id)
        .options(selectinload(Episode.season).selectinload(Season.series)))
    episode = result.scalar_one_or_none()
    if episode is None:
        raise HTTPException(status_code=404)
    if await items.coming(session, episode_id=episode.id):
        raise HTTPException(status_code=409, detail="already on its way")
    try:
        candidates = await search.search_candidates(config, episode=episode, session=session)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    best = grab.next_candidate(
        candidates, await grab.rejected_posts(session, episode_id=episode.id))
    if best is None:
        raise HTTPException(status_code=404, detail="no releases found")
    try:
        dl = await grab.grab(session, config, best, episode=episode)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"download_id": dl.id, "release": best.title, "channel": best.channel,
            "score": best.score}


@router.get("/episodes/{episode_id}/releases")
async def episode_releases(episode_id: int, session=Depends(get_session)):
    config = await current_runtime()
    result = await session.execute(
        select(Episode).where(Episode.id == episode_id)
        .options(selectinload(Episode.season).selectinload(Season.series)))
    episode = result.scalar_one_or_none()
    if episode is None:
        raise HTTPException(status_code=404)
    try:
        candidates = await search.search_candidates(config, episode=episode, session=session)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    said = await grab.standings(session, episode_id=episode.id)
    return [{**score.candidate_json(c), "standing": grab.standing(c, said)}
            for c in candidates]


@router.post("/episodes/{episode_id}/grab", status_code=202)
async def episode_grab(episode_id: int, body: ReleaseSelection, session=Depends(get_session)):
    config = await current_runtime()
    result = await session.execute(
        select(Episode).where(Episode.id == episode_id)
        .options(selectinload(Episode.season).selectinload(Season.series)))
    episode = result.scalar_one_or_none()
    if episode is None:
        raise HTTPException(status_code=404)
    if await items.coming(session, episode_id=episode.id):
        raise HTTPException(status_code=409, detail="already on its way")
    try:
        dl = await grab.grab(session, config, _candidate_from_selection(body), episode=episode)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"download_id": dl.id, "release": body.title, "channel": body.channel,
            "score": body.score}


_grabbing_seasons: set[tuple[int, int]] = set()


async def _grab_season(series_id: int, season_number: int) -> None:
    """Background: auto-grab the best release for every wanted, already-aired
    episode of one season (the season-level 'download' action).

    What is already on its way is read per episode rather than once at the top.
    The run takes a search per episode, so a list drawn at the start is stale
    long before the last line of it is reached — two runs started seconds apart
    both read an empty list and both grabbed the same episodes, and from there
    each failure answered itself twice all the way down the releases."""
    try:
        async with SessionLocal() as session:
            config = await current_runtime()
            result = await session.execute(
                select(Episode).join(Season).where(
                    Season.series_id == series_id, Season.number == season_number)
                .options(selectinload(Episode.files),
                         selectinload(Episode.season).selectinload(Season.series)))
            today = datetime.date.today()
            for e in result.scalars():
                if e.files or await items.coming(session, episode_id=e.id):
                    continue
                if e.air_date and e.air_date > today:
                    continue  # not aired yet
                try:
                    candidates = await search.search_candidates(
                        config, episode=e, session=session)
                    choice = grab.next_candidate(
                        candidates, await grab.rejected_posts(session, episode_id=e.id))
                    if choice is not None:
                        await grab.grab(session, config, choice, episode=e)
                except AcquireError as exc:
                    log.warning("season grab: episode %s failed: %s", e.id, exc)
    finally:
        _grabbing_seasons.discard((series_id, season_number))


@router.patch("/series/{series_id}/seasons/{season_number}")
async def season_patch(series_id: int, season_number: int, body: SeasonPatch,
                       session=Depends(get_session)):
    """Whether the monitor pass follows this one season.

    The flag has always been in the table and the pass has always obeyed it;
    what was missing was a way to say it. Holding a series and wanting every
    season of it are two different sentences, and a caller offering the choice
    needs the second one."""
    season = (await session.execute(
        select(Season).where(Season.series_id == series_id,
                             Season.number == season_number))).scalar_one_or_none()
    if season is None:
        raise HTTPException(status_code=404)
    season.monitored = body.monitored
    await session.commit()
    return {"ok": True, "monitored": season.monitored}


@router.post("/series/{series_id}/seasons/{season_number}/search", status_code=202)
async def season_search(series_id: int, season_number: int, session=Depends(get_session)):
    result = await session.execute(
        select(Episode).join(Season).where(
            Season.series_id == series_id, Season.number == season_number)
        .options(selectinload(Episode.files)))
    active = await items.active_ids(session, VideoDownload.episode_id)
    today = datetime.date.today()
    wanted = [e for e in result.scalars()
              if not e.files and e.id not in active
              and not (e.air_date and e.air_date > today)]
    if wanted and (series_id, season_number) not in _grabbing_seasons:
        _grabbing_seasons.add((series_id, season_number))
        passes.spawn(_grab_season(series_id, season_number))
    return {"queued": len(wanted)}
