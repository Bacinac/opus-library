"""TMDB is the canonical catalog for movies and series — what Deezer is to the
music half. One free API key covers search, details, seasons/episodes and
images. Both v3 keys and v4 read tokens are accepted."""

import asyncio
import datetime
import logging

import httpx

log = logging.getLogger("opus.tmdb")

BASE = "https://api.themoviedb.org/3"
POSTER = "https://image.tmdb.org/t/p/w342"
BACKDROP = "https://image.tmdb.org/t/p/w780"
STILL = "https://image.tmdb.org/t/p/w300"
PROFILE = "https://image.tmdb.org/t/p/w185"
LOGO = "https://image.tmdb.org/t/p/w92"
# watch-provider region (JustWatch data); Croatia
REGION = "HR"

# What TMDB calls popular television is mostly what airs every day: /tv/popular
# opens on Paradise Hotel, a Hong Kong daily sitcom and Sesame Street. Kids,
# news, reality, soap and talk go, and so does a show too few people have rated
# to be known — TMDB's genres miss half the daily soaps, the vote count does not.
TV_DAILY_GENRES = "10762|10763|10764|10766|10767"
TV_KNOWN_VOTES = 300
TV_PREMIERE_DAYS = 60


class TmdbError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _auth(config) -> tuple[dict, dict]:
    """Returns (params, headers) for the configured key: v4 read tokens are
    JWTs sent as Bearer, classic v3 keys go in the query string."""
    key = config.get("tmdb_api_key")
    if not key:
        raise TmdbError("TMDB API key is not set")
    if key.startswith("eyJ"):
        return {}, {"Authorization": f"Bearer {key}"}
    return {"api_key": key}, {}


# One pool for the process: a client per request pays a TLS handshake per request.
_pool: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None


def _client() -> httpx.AsyncClient:
    global _pool
    loop = asyncio.get_running_loop()
    if _pool is None or _pool[0] is not loop or _pool[1].is_closed:
        _pool = (loop, httpx.AsyncClient(timeout=30))
    return _pool[1]


async def close() -> None:
    global _pool
    if _pool is not None and _pool[0] is asyncio.get_running_loop():
        await _pool[1].aclose()
    _pool = None


async def _get(config, path: str, **params) -> dict:
    """A v3 key can only travel in the query string, and httpx writes the whole
    address into its errors — so what is raised from here is said in our own
    words, and the original is not chained where a traceback would print it."""
    auth_params, headers = _auth(config)
    try:
        resp = await _client().get(f"{BASE}{path}", params={**auth_params, **params},
                                   headers=headers)
    except httpx.HTTPError as exc:
        raise TmdbError(f"TMDB {path} unreachable: {type(exc).__name__}") from None
    if resp.is_error:
        raise TmdbError(f"TMDB {path} answered {resp.status_code}", resp.status_code)
    try:
        return resp.json()
    except ValueError:
        raise TmdbError(f"TMDB {path} answered something that is not JSON") from None


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and len(date) >= 4 else None


def _date(date: str | None) -> datetime.date | None:
    return datetime.date.fromisoformat(date) if date else None


def _img(base: str, path: str | None) -> str | None:
    return f"{base}{path}" if path else None


async def health(config) -> tuple[bool, str]:
    try:
        await _get(config, "/configuration")
        return True, "TMDB"
    except TmdbError as exc:
        return False, str(exc)


async def search_movies(config, query: str) -> list[dict]:
    data = await _get(config, "/search/movie", query=query, include_adult="false")
    return [{
        "tmdb_id": r["id"],
        "title": r.get("title", ""),
        "original_title": r.get("original_title", ""),
        "year": _year(r.get("release_date")),
        "overview": r.get("overview", ""),
        "poster_url": _img(POSTER, r.get("poster_path")),
    } for r in data.get("results", [])]


async def search_series(config, query: str) -> list[dict]:
    data = await _get(config, "/search/tv", query=query, include_adult="false")
    return [{
        "tmdb_id": r["id"],
        "title": r.get("name", ""),
        "original_title": r.get("original_name", ""),
        "year": _year(r.get("first_air_date")),
        "overview": r.get("overview", ""),
        "poster_url": _img(POSTER, r.get("poster_path")),
    } for r in data.get("results", [])]


def _croatian(r: dict) -> str:
    """The Croatian overview out of the translations TMDB sends alongside the
    rest. Asking again with language=hr-HR would be a second request and would
    silently hand back the English one when no Croatian exists, which is a
    fallback that cannot be told apart from a translation."""
    for entry in (r.get("translations") or {}).get("translations", []):
        if entry.get("iso_639_1") == "hr":
            return (entry.get("data") or {}).get("overview") or ""
    return ""


CAST_SHOWN = 12


def _people(r: dict) -> dict:
    """Genres, whoever directed it and the faces at the top of the bill. The
    cast is cut at twelve — a details screen a person reads from a sofa shows
    the billing, not the crawl."""
    credits = r.get("credits") or r.get("aggregate_credits") or {}
    # named AND numbered: a name is what a reader sees and the id is what the
    # rest of their work is found by, the same as the billing
    directors = [{"id": c.get("id"), "name": c["name"]} for c in credits.get("crew", [])
                 if c.get("job") in ("Director", "Series Director")]
    if not directors:
        directors = [{"id": c.get("id"), "name": c["name"]} for c in r.get("created_by", [])]
    cast = []
    for c in credits.get("cast", [])[:CAST_SHOWN]:
        # tv aggregate credits carry the character under roles[], film under character
        roles = c.get("roles") or []
        cast.append({
            "id": c.get("id"),
            "name": c.get("name", ""),
            "character": (roles[0].get("character") if roles else c.get("character")) or "",
            "profile_url": _img(PROFILE, c.get("profile_path")),
        })
    return {
        "genres": [g["name"] for g in r.get("genres", [])],
        # who made it, and the mark they made it under: a studio is a logo
        # everywhere it appears in the world except in a list of names
        "studios": [{"id": c.get("id"), "name": c["name"],
                     "logo": _img(LOGO, c.get("logo_path"))}
                    for c in r.get("production_companies", [])],
        "directors": directors,
        "cast": cast,
    }


async def movie_extras(config, tmdb_id: int) -> dict:
    r = await _get(config, f"/movie/{tmdb_id}",
                   append_to_response="credits,translations")
    return {**_people(r), "overview_hr": _croatian(r)}


async def series_extras(config, tmdb_id: int) -> dict:
    """The billing and the Croatian overview, and nothing else. series_details()
    costs a request per season, which is the wrong price for a row that already
    has its seasons."""
    r = await _get(config, f"/tv/{tmdb_id}",
                   append_to_response="aggregate_credits,translations")
    return {**_people(r), "overview_hr": _croatian(r)}


async def movie_details(config, tmdb_id: int) -> dict:
    r = await _get(config, f"/movie/{tmdb_id}",
                   append_to_response="external_ids,credits,translations")
    return {
        "tmdb_id": r["id"],
        "imdb_id": r.get("external_ids", {}).get("imdb_id") or r.get("imdb_id"),
        "title": r.get("title", ""),
        "original_title": r.get("original_title", ""),
        "year": _year(r.get("release_date")),
        "overview": r.get("overview", ""),
        "overview_hr": _croatian(r),
        "poster_url": _img(POSTER, r.get("poster_path")),
        "backdrop_url": _img(BACKDROP, r.get("backdrop_path")),
        "runtime_min": r.get("runtime"),
        **_people(r),
    }


async def season_in_croatian(config, tmdb_id: int, number: int) -> tuple[str, dict[int, str]]:
    """One season as TMDB has it in Croatian: its own overview, and every
    episode's by episode number. A whole season in one request is what makes
    translating episodes affordable at all — asking per episode is several
    hundred calls a series."""
    try:
        data = await _get(config, f"/tv/{tmdb_id}/season/{number}", language="hr-HR")
    except TmdbError as exc:
        log.warning("TMDB hr season %s/%s failed: %s", tmdb_id, number, exc)
        return "", {}
    return data.get("overview", ""), {e.get("episode_number"): e.get("overview", "")
                                      for e in data.get("episodes", [])}


def season_said(detail: dict, croatian: str) -> dict:
    """What a season is and what it looks like, off TMDB's season listing.
    The poster is empty rather than absent when TMDB has none, so the season
    is not asked again."""
    return {"overview": detail.get("overview", ""),
            "overview_hr": translated(croatian, detail.get("overview", "")),
            "poster_url": _img(POSTER, detail.get("poster_path")) or ""}


def translated(croatian: str | None, english: str) -> str:
    """TMDB answers a Croatian request with the English text when it has no
    Croatian, and the two are indistinguishable until you compare them. Equal
    means untranslated, and untranslated is empty."""
    body = (croatian or "").strip()
    return "" if not body or body == (english or "").strip() else body


def pictured(e: dict) -> dict:
    """What an episode looks like and how long it runs, as TMDB's season
    listing carries them."""
    return {"still_url": _img(STILL, e.get("still_path")),
            "runtime_min": e.get("runtime") or None}


async def season_detail(config, tmdb_id: int, number: int) -> dict:
    return await _get(config, f"/tv/{tmdb_id}/season/{number}")


async def season_pictures(config, tmdb_id: int, number: int) -> dict[int, dict]:
    """The same for every episode of one season, by episode number."""
    detail = await season_detail(config, tmdb_id, number)
    return {e["episode_number"]: pictured(e) for e in detail.get("episodes", [])
            if e.get("episode_number") is not None}


async def series_details(config, tmdb_id: int) -> dict:
    r = await _get(config, f"/tv/{tmdb_id}",
                   append_to_response="aggregate_credits,translations")
    seasons = []
    for s in r.get("seasons", []):
        if s.get("season_number", 0) < 1:
            continue  # skip specials for the default layout
        detail = await _get(config, f"/tv/{tmdb_id}/season/{s['season_number']}")
        said, croatian = await season_in_croatian(config, tmdb_id, s["season_number"])
        seasons.append({
            "number": s["season_number"],
            **season_said(detail, said),
            "episodes": [{
                "tmdb_id": e.get("id"),
                "number": e.get("episode_number"),
                "title": e.get("name", ""),
                "air_date": _date(e.get("air_date")),
                "overview": e.get("overview", ""),
                "overview_hr": translated(croatian.get(e.get("episode_number")),
                                           e.get("overview", "")),
                **pictured(e),
            } for e in detail.get("episodes", [])],
        })
    return {
        "tmdb_id": r["id"],
        "title": r.get("name", ""),
        "original_title": r.get("original_name", ""),
        "year": _year(r.get("first_air_date")),
        "overview": r.get("overview", ""),
        "overview_hr": _croatian(r),
        "poster_url": _img(POSTER, r.get("poster_path")),
        "backdrop_url": _img(BACKDROP, r.get("backdrop_path")),
        "status": r.get("status", ""),
        "seasons": seasons,
        **_people(r),
    }


# ------------------------------------------------------------- discovery ----

def _card(r: dict) -> dict:
    """Normalize a movie/tv result (search, trending, discover, credits) into a
    compact card."""
    mt = r.get("media_type")
    if mt not in ("movie", "tv"):
        mt = "movie" if ("title" in r or "release_date" in r) else "tv"
    return {
        "tmdb_id": r.get("id"),
        "media_type": mt,
        "title": r.get("title") or r.get("name", ""),
        "year": _year(r.get("release_date") or r.get("first_air_date")),
        "poster_url": _img(POSTER, r.get("poster_path")),
        "overview": r.get("overview", ""),
        "vote_average": round(r.get("vote_average") or 0, 1),
    }


async def trending(config, media_type: str = "all", window: str = "week") -> list[dict]:
    data = await _get(config, f"/trending/{media_type}/{window}")
    return [_card(r) for r in data.get("results", [])
            if r.get("id") and r.get("media_type") in (None, "movie", "tv")]


async def streaming(config, media_type: str, tmdb_id: int) -> list[dict]:
    """The subscription services a title is on in this region, in JustWatch's
    order. Renting, buying and the free and ad-carried listings are not a
    subscription."""
    try:
        data = await _get(config, f"/{media_type}/{tmdb_id}/watch/providers")
    except TmdbError as exc:
        if exc.status == 404:
            return []
        raise
    offers = (data.get("results") or {}).get(REGION) or {}
    return [{"id": p["provider_id"], "name": p.get("provider_name", ""),
             "logo": _img(LOGO, p.get("logo_path"))}
            for p in sorted(offers.get("flatrate") or [],
                            key=lambda p: p.get("display_priority") or 0)]


async def card(config, media_type: str, tmdb_id: int) -> dict:
    return _card({**await _get(config, f"/{media_type}/{tmdb_id}"), "media_type": media_type})


async def find_by_imdb(config, imdb_id: str) -> dict[str, int]:
    data = await _get(config, f"/find/{imdb_id}", external_source="imdb_id")
    return {media_type: found[0]["id"] for media_type in ("movie", "tv")
            if (found := data.get(f"{media_type}_results"))}


async def popular(config, media_type: str) -> list[dict]:
    if media_type == "tv":
        data = await _get(config, "/discover/tv", sort_by="popularity.desc", include_adult="false",
                          without_genres=TV_DAILY_GENRES, **{"vote_count.gte": TV_KNOWN_VOTES})
    else:
        data = await _get(config, "/movie/popular")
    return [_card({**r, "media_type": media_type}) for r in data.get("results", [])]


async def upcoming(config, media_type: str) -> list[dict]:
    """Films about to come out, and series about to begin. /tv/on_the_air is
    everything with an episode this week — the talk shows, not what is coming."""
    if media_type == "tv":
        today = datetime.date.today()
        data = await _get(config, "/discover/tv", sort_by="popularity.desc", include_adult="false",
                          without_genres=TV_DAILY_GENRES, **{
                              "first_air_date.gte": (today + datetime.timedelta(days=1)).isoformat(),
                              "first_air_date.lte": (today + datetime.timedelta(days=TV_PREMIERE_DAYS)).isoformat()})
    else:
        data = await _get(config, "/movie/upcoming")
    return [_card({**r, "media_type": media_type}) for r in data.get("results", [])]


async def discover(config, media_type: str, genre: str | None = None,
                   sort: str = "popularity.desc", year: int | None = None) -> list[dict]:
    params: dict = {"sort_by": sort, "include_adult": "false", "watch_region": REGION,
                    "vote_count.gte": 20}
    if genre:
        params["with_genres"] = genre
    if year:
        params["primary_release_year" if media_type == "movie" else "first_air_date_year"] = year
    data = await _get(config, f"/discover/{media_type}", **params)
    return [_card({**r, "media_type": media_type}) for r in data.get("results", [])]


async def genres(config, media_type: str) -> list[dict]:
    data = await _get(config, f"/genre/{media_type}/list")
    return [{"id": g["id"], "name": g["name"]} for g in data.get("genres", [])]


async def company_credits(config, company_id: int) -> dict:
    """What a studio made, most talked-about first. TMDB answers companies
    through discover rather than through a credits list, which is the same
    question asked the other way round."""
    company = await _get(config, f"/company/{company_id}")
    films = await _get(config, "/discover/movie", with_companies=company_id,
                       sort_by="popularity.desc")
    shows = await _get(config, "/discover/tv", with_companies=company_id,
                       sort_by="popularity.desc")
    credits = [_card({**c, "media_type": "movie"}) for c in films.get("results", [])]
    credits += [_card({**c, "media_type": "tv"}) for c in shows.get("results", [])]
    return {"id": company.get("id"), "name": company.get("name", ""),
            "profile_url": _img(LOGO, company.get("logo_path")),
            "about": (company.get("description") or "").strip(),
            "born": None, "died": None,
            "from": company.get("headquarters") or company.get("origin_country") or "",
            "credits": credits}


async def person_credits(config, person_id: int) -> dict:
    r = await _get(config, f"/person/{person_id}", append_to_response="combined_credits")
    seen: set = set()
    credits = []
    combined = r.get("combined_credits", {}) or {}
    # Somebody's work is what they acted in AND what they directed. Asking only
    # the cast list answered nothing at all for a director, who appears in the
    # crew of everything they made and the cast of none of it.
    both = (combined.get("cast") or []) + [
        c for c in (combined.get("crew") or [])
        if c.get("job") in ("Director", "Series Director", "Creator")
    ]
    for c in sorted(both, key=lambda x: x.get("popularity") or 0, reverse=True):
        if c.get("media_type") not in ("movie", "tv") or c.get("id") in seen:
            continue
        # drop talk-show / self appearances — keep the real filmography
        if "self" in (c.get("character") or "").lower():
            continue
        seen.add(c["id"])
        credits.append(_card(c))
    return {"id": r.get("id"), "name": r.get("name", ""),
            "profile_url": _img(PROFILE, r.get("profile_path")),
            # who they are, above what they made
            "about": (r.get("biography") or "").strip(),
            "born": r.get("birthday"), "died": r.get("deathday"),
            "from": r.get("place_of_birth") or "",
            "credits": credits}


async def season_episodes(config, tmdb_id: int, number: int) -> list[dict]:
    """One season of a series nobody holds yet, episode by episode. Asked for a
    season at a time because that is how a chooser reveals one."""
    detail = await _get(config, f"/tv/{tmdb_id}/season/{number}")
    return [{
        "number": e.get("episode_number"),
        "title": e.get("name", ""),
        "air_date": _date(e.get("air_date")),
        "overview": e.get("overview", ""),
    } for e in detail.get("episodes", [])]


async def series_seasons(config, tmdb_id: int) -> dict:
    """What seasons a series has, before anyone has decided to hold it: enough
    to choose among them."""
    r = await _get(config, f"/tv/{tmdb_id}")
    return {
        "tmdb_id": r["id"],
        "title": r.get("name", ""),
        "seasons": [
            {"number": s["season_number"], "episodes": s.get("episode_count") or 0,
             "year": _year(s.get("air_date"))}
            for s in r.get("seasons", []) if s.get("season_number", 0) >= 1
        ],
    }
