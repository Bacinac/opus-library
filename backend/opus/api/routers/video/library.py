"""The video library as it is on disk: what a scan found, what the whole of it
adds up to, and the two answers a human gives an unmatched folder — this is
that film, or stop asking about it."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from opus.api.routers.video.shared import held_langs
from opus.db import get_session
from opus.models import Episode, Movie, Season, Series, Subtitle, VideoFile
from opus.settings_store import current_runtime
from opus.video import library_scan, metadata_backfill
from opus.video.metadata import tmdb
from opus.video.subtitles import sync, timing
from opus.video.subtitles.policy import policy_met

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/library/scan", status_code=202)
async def library_scan_start():
    if not library_scan.job.start(library_scan.scan):
        raise HTTPException(status_code=409, detail="scan already running")
    return {"status": "started"}


@router.get("/library/scan")
async def library_scan_status():
    return library_scan.job.state


@router.post("/library/metadata", status_code=202)
async def library_metadata():
    """Fill in genres, director, billing and the Croatian overview for what was
    shelved before those were kept. Anything added since arrives with them."""
    if not metadata_backfill.job.start(metadata_backfill.fill):
        raise HTTPException(status_code=409, detail="already running")
    return {"status": "started"}


@router.get("/library/metadata")
async def library_metadata_status():
    return metadata_backfill.job.state


@router.post("/library/refetch-subs", status_code=202)
async def library_refetch_subs():
    """Measure every sidecar subtitle against the file it sits beside, throw out
    the ones written for another release, and ask the providers again.

    A search answers about the title, not about this copy of it, so a film held
    as one cut collects subtitles timed for another — they drift from the first
    minute and no player can rescue them. Only files that lost a subtitle are
    asked about again."""
    if not library_scan.resub.start(library_scan.resubtitle):
        raise HTTPException(status_code=409, detail="already running")
    return {"status": "started"}


@router.get("/library/refetch-subs")
async def library_refetch_subs_state():
    return library_scan.resub.state


class AlignBody(BaseModel):
    subtitle_id: int


@router.post("/library/align-subtitle")
async def library_align_subtitle(body: AlignBody, session=Depends(get_session)):
    """Re-time one sidecar subtitle onto its file's English track.

    Deliberately per-subtitle and deliberately not part of acquisition. Aligning
    answers "when", never "whether": handed a subtitle of another film it fits
    that one just as neatly, and the fitting erases the disagreement in running
    time that the acceptance test reads. So it is offered for a subtitle a
    person has already vouched for — the right translation, the wrong clock."""
    sub = await session.get(Subtitle, body.subtitle_id)
    if sub is None or not sub.path:
        raise HTTPException(status_code=404, detail="no such sidecar subtitle")
    media = await session.get(VideoFile, sub.file_id)
    tracks = [{"stream_index": s.stream_index, "lang": s.lang, "format": s.format}
              for s in (await session.execute(
                  select(Subtitle).where(Subtitle.file_id == sub.file_id,
                                         Subtitle.source == "embedded"))).scalars()]
    reference, came_from = await asyncio.to_thread(
        sync.reference_cues, media.path, tracks)
    if not reference:
        raise HTTPException(
            status_code=422,
            detail="this file carries no subtitle track to take a clock from")
    result = await asyncio.to_thread(sync.align, sub.path, reference)
    if result.status == "aligned":
        sub.vtt_path = sub.path
        await session.commit()
    fit = timing.check(sub.path, media.duration_s)
    return {"status": result.status, "detail": result.detail,
            "coverage_before": round(result.before, 3),
            "coverage_after": round(result.after, 3),
            "median_distance": None if result.distance == float("inf") else round(result.distance, 2),
            "reference": came_from, "reference_cues": len(reference),
            "fits_running_time": not fit.mismatch,
            "ratio": None if fit.ratio is None else round(fit.ratio, 3)}


@router.get("/library/stats")
async def library_stats(session=Depends(get_session)):
    """Whole-library totals (not just the last scan run): films, series, owned
    episodes, and for films and for episodes how many are complete and how many
    are waiting for subtitles."""
    config = await current_runtime()
    held = held_langs(config.bool("accept_auto_subs"))

    movies = (await session.execute(
        select(Movie.subtitle_override, held)
        .join(VideoFile, VideoFile.movie_id == Movie.id)
        .outerjoin(Subtitle, Subtitle.file_id == VideoFile.id)
        .group_by(Movie.id, Movie.subtitle_override)
    )).all()
    episodes = (await session.execute(
        select(Series.subtitle_override, held)
        .select_from(Episode)
        .join(VideoFile, VideoFile.episode_id == Episode.id)
        .join(Season, Season.id == Episode.season_id)
        .join(Series, Series.id == Season.series_id)
        .outerjoin(Subtitle, Subtitle.file_id == VideoFile.id)
        .group_by(Episode.id, Series.subtitle_override)
    )).all()

    def split(rows) -> dict:
        complete = sum(policy_met(config, override, langs) for override, langs in rows)
        return {"complete": complete, "waiting": len(rows) - complete}

    return {
        "movie_count": await session.scalar(select(func.count()).select_from(Movie)) or 0,
        "series_count": await session.scalar(select(func.count()).select_from(Series)) or 0,
        "episodes": len(episodes),
        "movies": split(movies),
        "series": split(episodes),
    }


class ResolveBody(BaseModel):
    path: str
    media_type: str  # movie | tv
    tmdb_id: int


class IgnoreBody(BaseModel):
    path: str


@router.post("/library/resolve", status_code=201)
async def library_resolve(body: ResolveBody, session=Depends(get_session)):
    if body.media_type not in ("movie", "tv"):
        raise HTTPException(status_code=422, detail="media_type must be movie or tv")
    config = await current_runtime()
    try:
        result = await library_scan.resolve_path(
            session, config, body.path, body.media_type, body.tmdb_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found")
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    library_scan.drop_result(body.path)
    return result


@router.post("/library/ignore", status_code=204)
async def library_ignore(body: IgnoreBody, session=Depends(get_session)):
    await library_scan.add_ignored(session, body.path)
    library_scan.drop_result(body.path)
