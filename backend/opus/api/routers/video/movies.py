"""Films: what the library holds, what it is missing, and the two ways to fetch
one — let the pipeline pick the best release, or hand it the one a human chose
in the picker."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from opus.acquire import AcquireError
from opus.api.routers.video.shared import (
    AddByTmdbId,
    ItemPatch,
    ReleaseSelection,
    _candidate_from_selection,
    _file_json,
    _movie_json,
    _movie_status,
    _playback_json,
    overview_in,
)
from opus.db import get_session
from opus.models import Movie, VideoDownload, VideoFile
from opus.settings_store import current_runtime
from opus.video.pipeline import grab, items, score, search
from opus.video.metadata import tmdb

router = APIRouter()


@router.get("/movies")
async def movies_list(lang: str = "en", session=Depends(get_session)):
    config = await current_runtime()
    result = await session.execute(
        select(Movie).options(selectinload(Movie.files).selectinload(VideoFile.subtitles))
        .order_by(Movie.added_at.desc()))
    movies = list(result.scalars())
    active = await items.active_ids(session, VideoDownload.movie_id)
    return [_movie_json(m, _movie_status(config, m, active), lang) for m in movies]


@router.get("/movies/{movie_id}")
async def movie_detail(movie_id: int, lang: str = "en", session=Depends(get_session)):
    config = await current_runtime()
    result = await session.execute(
        select(Movie).where(Movie.id == movie_id)
        .options(selectinload(Movie.files).selectinload(VideoFile.subtitles),
                 selectinload(Movie.files).selectinload(VideoFile.streams),
                 selectinload(Movie.files).selectinload(VideoFile.chapters)))
    movie = result.scalar_one_or_none()
    if movie is None:
        raise HTTPException(status_code=404)
    active = await items.active_ids(session, VideoDownload.movie_id)
    data = _movie_json(movie, _movie_status(config, movie, active), lang)
    data["files"] = [_file_json(f) for f in movie.files]
    return data


@router.get("/movies/{movie_id}/playback")
async def movie_playback(movie_id: int, lang: str = "en", session=Depends(get_session)):
    """Everything a player needs to decide HOW to play this, and nothing about
    how the library found it.

    A consumer that can reach the same trees can play; one that cannot gets a
    path it cannot use, which fails loudly rather than silently serving the
    wrong thing."""
    result = await session.execute(
        select(Movie).where(Movie.id == movie_id)
        .options(selectinload(Movie.files).selectinload(VideoFile.subtitles),
                 selectinload(Movie.files).selectinload(VideoFile.streams),
                 selectinload(Movie.files).selectinload(VideoFile.chapters)))
    movie = result.scalar_one_or_none()
    if movie is None:
        raise HTTPException(status_code=404)
    if not movie.files:
        raise HTTPException(status_code=409, detail="nothing has been imported for this film")
    return {
        "title": movie.title,
        "year": movie.year,
        "runtime_min": movie.runtime_min,
        "backdrop_url": movie.backdrop_url,
        "details": {"overview": overview_in(movie, lang), "genres": movie.genres,
                    "directors": movie.directors, "cast": movie.cast},
        **_playback_json(movie.files[0]),
    }


@router.post("/movies", status_code=201)
async def movies_add(body: AddByTmdbId, session=Depends(get_session)):
    config = await current_runtime()
    existing = await session.execute(select(Movie).where(Movie.tmdb_id == body.tmdb_id))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="already added")
    try:
        details = await tmdb.movie_details(config, body.tmdb_id)
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    movie = Movie(**details)
    session.add(movie)
    await session.commit()
    return {"id": movie.id, "title": movie.title, "year": movie.year}


@router.patch("/movies/{movie_id}")
async def movies_patch(movie_id: int, body: ItemPatch, session=Depends(get_session)):
    movie = await session.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(status_code=404)
    if body.monitored is not None:
        movie.monitored = body.monitored
    if body.subtitle_override is not None:
        movie.subtitle_override = body.subtitle_override
    await session.commit()
    return {"ok": True}


@router.delete("/movies/{movie_id}", status_code=204)
async def movies_delete(movie_id: int, session=Depends(get_session)):
    await session.execute(delete(Movie).where(Movie.id == movie_id))
    await session.commit()


@router.post("/movies/{movie_id}/search", status_code=202)
async def movies_search(movie_id: int, session=Depends(get_session)):
    config = await current_runtime()
    movie = await session.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(status_code=404)
    if await items.coming(session, movie_id=movie.id):
        raise HTTPException(status_code=409, detail="already on its way")
    try:
        candidates = await search.search_candidates(config, movie=movie, session=session)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    best = grab.next_candidate(
        candidates, await grab.rejected_posts(session, movie_id=movie.id))
    if best is None:
        raise HTTPException(status_code=404, detail="no releases found")
    try:
        dl = await grab.grab(session, config, best, movie=movie)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"download_id": dl.id, "release": best.title, "channel": best.channel,
            "score": best.score}


@router.get("/movies/{movie_id}/releases")
async def movie_releases(movie_id: int, session=Depends(get_session)):
    config = await current_runtime()
    movie = await session.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(status_code=404)
    try:
        candidates = await search.search_candidates(config, movie=movie, session=session)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    said = await grab.standings(session, movie_id=movie.id)
    return [{**score.candidate_json(c), "standing": grab.standing(c, said)}
            for c in candidates]


@router.post("/movies/{movie_id}/grab", status_code=202)
async def movie_grab(movie_id: int, body: ReleaseSelection, session=Depends(get_session)):
    config = await current_runtime()
    movie = await session.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(status_code=404)
    if await items.coming(session, movie_id=movie.id):
        raise HTTPException(status_code=409, detail="already on its way")
    try:
        dl = await grab.grab(session, config, _candidate_from_selection(body), movie=movie)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"download_id": dl.id, "release": body.title, "channel": body.channel,
            "score": body.score}
