"""Filling in, for what was already on the shelves, what is now kept about it.

Everything added from now on arrives with its genres, director, billing and
Croatian overview, because the TMDB call that fetches a title fetches them in
the same request. What was adopted or added before those columns existed has
empty ones, and this walks those rows once and asks."""

import asyncio
import logging

from sqlalchemy import and_, func, or_, select

from opus.db import SessionLocal
from opus.passes import Pass
from opus.models import Episode, Movie, Season, Series
from opus.settings_store import current_runtime
from opus.video.metadata import tmdb

log = logging.getLogger("opus.metadata")

job = Pass("video metadata", timed=False, done=0, total=0, current="")


async def fill() -> None:
    state = job.state
    async with SessionLocal() as session:
        config = await current_runtime()
        rows = []
        for model, fetch in ((Movie, tmdb.movie_extras),
                             (Series, tmdb.series_extras)):
            found = (await session.execute(select(model).where(
                or_(model.cast == [], model.genres == [], model.studios == [],
                    model.directors == [],
                    # studios written down before they carried their mark:
                    # the first of them has no logo key at all
                    and_(func.jsonb_array_length(model.studios) > 0,
                         model.studios[0]["logo"].astext.is_(None)),
                    model.overview_hr == "")))).scalars().all()
            rows.extend((model, item.id, item.tmdb_id, item.title, fetch)
                        for item in found)
        state["total"] = len(rows)

    for model, row_id, tmdb_id, title, fetch in rows:
        state["current"] = title
        try:
            details = await fetch(config, tmdb_id)
        except tmdb.TmdbError as exc:
            log.warning("metadata: TMDB failed for %s: %s", title, exc)
            continue
        async with SessionLocal() as session:
            item = await session.get(model, row_id)
            for field in ("genres", "studios", "directors", "cast", "overview_hr"):
                if details.get(field):
                    setattr(item, field, details[field])
            await session.commit()
        state["done"] += 1
        await asyncio.sleep(0.1)  # TMDB is generous but not infinite
    await _episodes_in_croatian(config)
    await _episode_pictures(config)
    await _season_words(config)
    log.info("metadata backfill: %d of %d filled", state["done"], state["total"])


async def _episodes_in_croatian(config) -> None:
    """The same for episodes, a season at a time. Whether a season is worth
    asking about is decided by whether any of its episodes still lacks a
    Croatian overview — a series done once is not asked again."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Series.tmdb_id, Season.id, Season.number, Series.title)
            .join(Season, Season.series_id == Series.id)
            .join(Episode, Episode.season_id == Season.id)
            .where(Episode.overview_hr == "", Episode.overview != "")
            .distinct())).all()

    for tmdb_id, season_id, number, title in rows:
        job.state["current"] = f"{title} · {number}"
        _, croatian = await tmdb.season_in_croatian(config, tmdb_id, number)
        if not croatian:
            continue
        async with SessionLocal() as session:
            episodes = (await session.execute(
                select(Episode).where(Episode.season_id == season_id))).scalars().all()
            for episode in episodes:
                said = tmdb.translated(croatian.get(episode.number), episode.overview)
                if said:
                    episode.overview_hr = said
            await session.commit()
        await asyncio.sleep(0.1)


async def _episode_pictures(config) -> None:
    """Each episode's still and running time, a season at a time, for the
    seasons that have an episode without a still."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Series.tmdb_id, Season.id, Season.number, Series.title)
            .join(Season, Season.series_id == Series.id)
            .join(Episode, Episode.season_id == Season.id)
            .where(Episode.still_url.is_(None))
            .distinct())).all()

    for tmdb_id, season_id, number, title in rows:
        job.state["current"] = f"{title} · {number}"
        try:
            pictures = await tmdb.season_pictures(config, tmdb_id, number)
        except tmdb.TmdbError as exc:
            log.warning("metadata: TMDB season %s/%s failed: %s", tmdb_id, number, exc)
            continue
        async with SessionLocal() as session:
            episodes = (await session.execute(
                select(Episode).where(Episode.season_id == season_id))).scalars().all()
            for episode in episodes:
                found = pictures.get(episode.number, {})
                episode.still_url = found.get("still_url") or episode.still_url
                episode.runtime_min = found.get("runtime_min") or episode.runtime_min
            await session.commit()
        await asyncio.sleep(0.1)


async def _season_words(config) -> None:
    """Each season's own overview and poster, for the seasons never asked."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Series.tmdb_id, Season.id, Season.number, Series.title)
            .join(Season, Season.series_id == Series.id)
            .where(Season.poster_url.is_(None)))).all()

    for tmdb_id, season_id, number, title in rows:
        job.state["current"] = f"{title} · {number}"
        try:
            detail = await tmdb.season_detail(config, tmdb_id, number)
        except tmdb.TmdbError as exc:
            log.warning("metadata: TMDB season %s/%s failed: %s", tmdb_id, number, exc)
            continue
        said, _ = await tmdb.season_in_croatian(config, tmdb_id, number)
        async with SessionLocal() as session:
            season = await session.get(Season, season_id)
            for field, value in tmdb.season_said(detail, said).items():
                setattr(season, field, value)
            await session.commit()
        await asyncio.sleep(0.1)
