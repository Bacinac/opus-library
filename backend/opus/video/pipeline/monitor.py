"""Followed series and films: new episodes are wanted as they air."""

import datetime
import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from opus import schedule
from opus.acquire import AcquireError
from opus.config import settings
from opus.db import SessionLocal
from opus.video.metadata import tmdb
from opus.models import VideoDownload, Episode, Movie, Season, Series
from opus.settings_store import RuntimeConfig, current_runtime
from opus.video.pipeline import grab, items, search

log = logging.getLogger("opus.video.pipeline")


async def _refresh_series_episodes(session, config: RuntimeConfig, series: Series) -> None:
    """Pull newly-announced seasons/episodes for a monitored series from TMDB so
    they can be grabbed once they air."""
    try:
        details = await tmdb.series_details(config, series.tmdb_id)
    except tmdb.TmdbError as exc:
        log.warning("monitor: TMDB refresh failed for %s: %s", series.title, exc)
        return
    for field in ("genres", "studios", "directors", "cast", "overview_hr"):
        if details.get(field):
            setattr(series, field, details[field])
    seasons_by_num = {s.number: s for s in series.seasons}
    for s in details.get("seasons", []):
        season = seasons_by_num.get(s["number"])
        existing: dict[int, Episode] = {}
        if season is None:
            season = Season(series_id=series.id, number=s["number"], monitored=True)
            session.add(season)
            await session.flush()
        else:
            existing = {e.number: e for e in season.episodes}
        # a season's poster and overview come after it is announced, like a still
        for field in ("overview", "overview_hr", "poster_url"):
            if s[field]:
                setattr(season, field, s[field])
        for e in s["episodes"]:
            if e["number"] in existing:
                # An episode is announced as "Episode 7" with no story and no
                # still; its name, overview, air date, still and length arrive
                # around the time it airs, so each is taken whenever TMDB has one
                known = existing[e["number"]]
                known.title = e["title"] or known.title
                known.overview = e["overview"] or known.overview
                known.overview_hr = e.get("overview_hr") or known.overview_hr
                known.air_date = e["air_date"] or known.air_date
                known.still_url = e["still_url"] or known.still_url
                known.runtime_min = e["runtime_min"] or known.runtime_min
                continue
            session.add(Episode(season_id=season.id, tmdb_id=e["tmdb_id"],
                                number=e["number"], title=e["title"],
                                air_date=e["air_date"], overview=e["overview"],
                                overview_hr=e.get("overview_hr", ""),
                                still_url=e.get("still_url"), runtime_min=e.get("runtime_min"),
                                monitored=True))
    await session.flush()


# Every series the pass has any business in: the ones being followed, and the
# ones not followed that hold a season somebody asked for anyway.
_FOLLOWED = or_(Series.monitored.is_(True),
                Series.id.in_(select(Season.series_id).where(Season.monitored.is_(True))))


async def _monitor_pass(session, config: RuntimeConfig) -> None:
    today = datetime.date.today()

    result = await session.execute(
        select(Series).where(_FOLLOWED).options(
            selectinload(Series.seasons).selectinload(Season.episodes)))
    for series in result.scalars():
        await _refresh_series_episodes(session, config, series)
    await session.commit()

    result = await session.execute(
        select(Series).where(_FOLLOWED).options(
            selectinload(Series.seasons).selectinload(Season.episodes).selectinload(Episode.files)))
    active_ep = await items.active_ids(session, VideoDownload.episode_id)
    for series in result.scalars():
        for season in series.seasons:
            # a series being followed does not make every episode of it wanted.
            # The library holds whole runs nobody asked for — the season and the
            # episode each say so, and chasing what they exclude would fetch a
            # thousand old episodes the first time the loop ran.
            if not season.monitored:
                continue
            for e in season.episodes:
                if not items.wanted(e) or e.files or e.id in active_ep:
                    continue
                if not e.air_date or e.air_date > today:
                    continue
                try:
                    candidates = await search.search_candidates(
                        config, episode=await items.episode_with_series(session, e.id),
                        session=session)
                    choice = grab.next_candidate(
                        candidates, await grab.rejected_posts(session, episode_id=e.id))
                    if choice is not None:
                        await grab.grab(session, config, choice, episode=e)
                        log.info("monitor: grabbed %s S%02dE%02d — %s",
                                 series.title, season.number, e.number, choice.title)
                    elif candidates:
                        log.warning("monitor: every release for %s S%02dE%02d has already failed",
                                    series.title, season.number, e.number)
                except AcquireError as exc:
                    log.warning("monitor: episode %s grab failed: %s", e.id, exc)

    # released monitored movies with no file: search keeps returning nothing
    # until a release exists, then it grabs
    result = await session.execute(
        select(Movie).where(Movie.monitored.is_(True)).options(selectinload(Movie.files)))
    active_m = await items.active_ids(session, VideoDownload.movie_id)
    for movie in result.scalars():
        if movie.files or movie.id in active_m:
            continue
        try:
            candidates = await search.search_candidates(config, movie=movie, session=session)
            choice = grab.next_candidate(
                candidates, await grab.rejected_posts(session, movie_id=movie.id))
            if choice is not None:
                await grab.grab(session, config, choice, movie=movie)
                log.info("monitor: grabbed movie %s — %s", movie.title, choice.title)
            elif candidates:
                log.warning("monitor: every release for movie %s has already failed", movie.title)
        except AcquireError as exc:
            log.warning("monitor: movie %s grab failed: %s", movie.id, exc)


async def monitor_loop():
    await schedule.every("video.monitor", datetime.timedelta(seconds=settings.monitor_interval_seconds),
                         _monitor_round)


async def _monitor_round():
    async with SessionLocal() as session:
        config = await current_runtime()
        ok, _ = await tmdb.health(config)
        if ok:
            await _monitor_pass(session, config)
