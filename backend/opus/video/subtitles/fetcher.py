"""Getting a language a file is missing, whoever is asking.

An import asks because the acceptance test failed; the library asks because an
audit threw a subtitle away for belonging to another cut. Both need the same
thing — the file's identity in a form a provider will answer to — so it is
worked out here rather than twice."""

import logging

from sqlalchemy import select

from opus.models import Episode, Movie, Season, Series, Subtitle, VideoFile
from opus.video.subtitles import opensubtitles

log = logging.getLogger(__name__)


async def _identity(session, media: VideoFile, query_hint: str) -> dict | None:
    """How to ask a provider about this file. A film is its imdb/tmdb id; an
    episode is its series plus a season and a number, never a release title —
    a provider handed 'Hustle.S05E06.1080p.WEB.H264-CBFM' matches whatever it
    decides that resembles."""
    if media.movie_id:
        movie = await session.get(Movie, media.movie_id)
        if movie is None:
            return None
        return {"imdb_id": movie.imdb_id, "tmdb_id": movie.tmdb_id, "query": movie.title}
    if media.episode_id:
        row = (await session.execute(
            select(Episode, Season, Series)
            .join(Season, Season.id == Episode.season_id)
            .join(Series, Series.id == Season.series_id)
            .where(Episode.id == media.episode_id))).first()
        if row is None:
            return None
        episode, season, series = row
        return {"parent_tmdb_id": series.tmdb_id, "season": season.number,
                "episode": episode.number,
                "query": f"{series.title} S{season.number:02d}E{episode.number:02d}"}
    return {"query": query_hint} if query_hint else None


async def acquire_missing(session, config, media: VideoFile, langs, *,
                          query_hint: str = "") -> list[dict]:
    """Try the providers for `langs` and persist what landed and fitted.
    Returns [{path, lang}]. QuotaExhausted is left to the caller — it means
    every further request this run would fail too."""
    if not opensubtitles.configured(config) or not langs:
        return []
    identity = await _identity(session, media, query_hint)
    if identity is None:
        return []
    try:
        saved = await opensubtitles.fetch(
            config, video_path=media.path, langs=list(langs),
            duration_s=media.duration_s, **identity)
    except opensubtitles.QuotaExhausted:
        raise
    except opensubtitles.SubtitleProviderError as exc:
        log.warning("subtitle acquisition failed for %s: %s", media.path, exc)
        return []
    for sub in saved:
        session.add(Subtitle(file_id=media.id, lang=sub["lang"],
                             source="opensubtitles", format="srt", path=sub["path"]))
    await session.flush()
    return saved
