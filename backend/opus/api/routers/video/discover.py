"""TMDB, which is the video half's catalogue: what is trending, what won, what a
person was in, what a title search turns up, where each of those streams, and
whether the library already holds it.

Nothing here touches the library except to answer that last question."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from opus.db import get_session
from opus.models import Movie, Series
from opus.settings_store import current_runtime
from opus.video import awards, streaming
from opus.video.metadata import tmdb
from opus.video.metadata.awards import AwardsError

router = APIRouter()


@router.get("/discover/trending")
async def discover_trending(type: str = "all", session=Depends(get_session)):
    config = await current_runtime()
    try:
        return await streaming.attach(session, config, await tmdb.trending(config, type))
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/discover/popular")
async def discover_popular(type: str = "movie", session=Depends(get_session)):
    config = await current_runtime()
    try:
        return await streaming.attach(session, config, await tmdb.popular(config, type))
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/discover/upcoming")
async def discover_upcoming(type: str = "movie", session=Depends(get_session)):
    config = await current_runtime()
    try:
        return await streaming.attach(session, config, await tmdb.upcoming(config, type))
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/discover/genres")
async def discover_genres(type: str = "movie"):
    config = await current_runtime()
    try:
        return await tmdb.genres(config, type)
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


def _models(kind: str):
    """Films, series, or both — asked from the shelf you are standing at."""
    if kind == "movie":
        return (Movie,)
    if kind == "tv":
        return (Series,)
    return (Movie, Series)


@router.get("/discover/held/people")
async def held_people(type: str = "", session=Depends(get_session)):
    """Everybody on the billing of anything we hold, most-held first. TMDB can
    say who is popular in the world; only this can say who is in the house."""
    seen: dict[int, dict] = {}
    for model in _models(type):
        for item in (await session.execute(select(model))).scalars():
            for person in item.cast or []:
                if not person.get("id"):
                    continue
                at = seen.setdefault(person["id"], {
                    "id": person["id"], "name": person.get("name", ""),
                    "profile_url": person.get("profile_url"), "held": 0})
                at["held"] += 1
    return sorted(seen.values(), key=lambda p: (-p["held"], p["name"]))


@router.get("/discover/held/studios")
async def held_studios(type: str = "", session=Depends(get_session)):
    """The studios behind what we hold. TMDB has no way to browse companies, so
    this is the only list of them there is."""
    seen: dict[int, dict] = {}
    for model in _models(type):
        for item in (await session.execute(select(model))).scalars():
            for studio in item.studios or []:
                if not studio.get("id"):
                    continue
                at = seen.setdefault(studio["id"], {
                    "id": studio["id"], "name": studio.get("name", ""), "held": 0})
                at["held"] += 1
    return sorted(seen.values(), key=lambda s: (-s["held"], s["name"]))


@router.get("/discover/browse")
async def discover_browse(type: str = "movie", genre: str | None = None,
                          sort: str = "popularity.desc", year: int | None = None,
                          session=Depends(get_session)):
    config = await current_runtime()
    try:
        found = await tmdb.discover(config, type, genre=genre, sort=sort, year=year)
        return await streaming.attach(session, config, found)
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/discover/company/{company_id}")
async def discover_company(company_id: int, session=Depends(get_session)):
    config = await current_runtime()
    try:
        found = await tmdb.company_credits(config, company_id)
        await streaming.attach(session, config, found["credits"])
        return found
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/discover/person/{person_id}")
async def discover_person(person_id: int, session=Depends(get_session)):
    config = await current_runtime()
    try:
        found = await tmdb.person_credits(config, person_id)
        await streaming.attach(session, config, found["credits"])
        return found
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/discover/awarded")
async def discover_awarded(type: str = "movie", session=Depends(get_session)):
    if type not in ("movie", "tv"):
        raise HTTPException(status_code=404)
    config = await current_runtime()
    try:
        found = await awards.awarded(session, config, type)
        await streaming.attach(session, config, found["titles"])
        return found
    except (tmdb.TmdbError, AwardsError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/discover/streaming")
async def discover_streaming(session=Depends(get_session)):
    return await streaming.services(session, await current_runtime())


@router.get("/discover/tv/{tmdb_id}/season/{number}")
async def discover_season(tmdb_id: int, number: int):
    """The episodes of one season of a series the library does not hold. Held,
    the tree under /video/series says this; unheld, TMDB is the only one who
    knows, and a chooser has to show what it is offering."""
    config = await current_runtime()
    try:
        return await tmdb.season_episodes(config, tmdb_id, number)
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/discover/tv/{tmdb_id}")
async def discover_series(tmdb_id: int, session=Depends(get_session)):
    config = await current_runtime()
    try:
        data = await tmdb.series_seasons(config, tmdb_id)
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    held = (await session.execute(
        select(Series.id).where(Series.tmdb_id == tmdb_id))).scalar_one_or_none()
    return {**data, "in_library": held is not None, "library_id": held}


# ---------------------------------------------------------------- search ----

@router.get("/search/movies")
async def search_movies(q: str, session=Depends(get_session)):
    config = await current_runtime()
    try:
        return await streaming.attach(session, config, await tmdb.search_movies(config, q), "movie")
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/search/series")
async def search_series(q: str, session=Depends(get_session)):
    config = await current_runtime()
    try:
        return await streaming.attach(session, config, await tmdb.search_series(config, q), "tv")
    except tmdb.TmdbError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
