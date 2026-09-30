"""OpenSubtitles REST API provider — the active-acquisition step when a
downloaded file lacks a required language. Needs an API key plus account
credentials (download quota is per-account). Croatian-focused providers
(Titlovi.com, Podnapisi) are planned as siblings behind the same function
shape."""

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx

from opus import http
from opus.video.subtitles import timing

log = logging.getLogger(__name__)

BASE = "https://api.opensubtitles.com/api/v1/"
APP_UA = "OPUS-Library v0.1.0"
# how many releases of one language to pay a download for before giving up on
# it. Every attempt costs quota, and a title whose first four candidates are
# all for another cut is a title the provider does not have this copy of
MAX_CANDIDATES = 4
HASH_CHUNK = 65536
# the provider meters downloads per second as well as per day, and refuses the
# second of two candidates tried back to back
RATE_LIMIT_BACKOFF = (1.5, 4.0)

# what the last download answer said was left of today's allowance; a long pass
# reads it to know whether it can afford the next file.
#
# It comes from the download answer and from nowhere else. `infos/user` reports
# a different and wrong number — it said 20 of 20 spent and 0 remaining at a
# moment when a download went through and answered `remaining: 62` — so a pass
# that trusted it would refuse to start on a full allowance. Until the first
# download of a run there is no honest figure, and None means exactly that.
spent: dict = {"remaining": None}

# the login endpoint allows one request per second per IP, and a pass over a
# library asks about files far faster than that. The token outlives the day, so
# it is taken once and reused; logging in per file cost twelve of them a
# provider that answered "Login rate limit exceeded" instead of a subtitle.
TOKEN_TTL = 3600
_session: dict = {"token": "", "at": 0.0}


class SubtitleProviderError(Exception):
    pass


class QuotaExhausted(SubtitleProviderError):
    pass


def configured(config) -> bool:
    return bool(config.get("opensubtitles_api_key")
                and config.get("opensubtitles_username")
                and config.get("opensubtitles_password"))


def _moviehash(path: str) -> str | None:
    """The provider's file fingerprint: the size plus the 64-bit words of the
    first and last 64 KB. It identifies this copy rather than this film, which
    is the whole difference between a subtitle that fits and one that does not
    — so it is worth 128 KB of reading before every search."""
    try:
        size = os.path.getsize(path)
        if size < HASH_CHUNK * 2:
            return None
        value = size
        with open(path, "rb") as fh:
            for offset in (0, size - HASH_CHUNK):
                fh.seek(offset)
                chunk = fh.read(HASH_CHUNK)
                for i in range(0, len(chunk) - 7, 8):
                    value = (value + int.from_bytes(chunk[i:i + 8], "little")) % (1 << 64)
        return f"{value:016x}"
    except OSError as exc:
        log.debug("could not hash %s: %s", path, exc)
        return None


async def _post(client: httpx.AsyncClient, path: str, ask: dict, **kwargs) -> httpx.Response:
    resp = await client.post(path, json=ask, **kwargs)
    for pause in RATE_LIMIT_BACKOFF:
        if resp.status_code != 429:
            break
        await asyncio.sleep(pause)
        resp = await client.post(path, json=ask, **kwargs)
    return resp


async def _login(client: httpx.AsyncClient, config) -> str:
    now = time.monotonic()
    if _session["token"] and now - _session["at"] < TOKEN_TTL:
        return _session["token"]
    resp = await _post(client, "login", {"username": config.get("opensubtitles_username"),
                                         "password": config.get("opensubtitles_password")})
    if resp.status_code != 200:
        raise SubtitleProviderError(f"OpenSubtitles login failed: {resp.status_code} {resp.text[:200]}")
    _session.update(token=resp.json()["token"], at=now)
    return _session["token"]


def _candidates(results: list[dict], lang: str) -> list[dict]:
    """The releases offering this language, best first: one the provider says
    matches this very file, then the one most people have taken.

    One release per name. The same rip is uploaded repeatedly under the same
    title, and paying a download to reject a copy of what was just rejected is
    quota spent to learn nothing."""
    picks = [r for r in results
             if (r.get("attributes") or {}).get("language") == lang
             and (r.get("attributes") or {}).get("files")]
    picks.sort(key=lambda r: (bool(r["attributes"].get("moviehash_match")),
                              r["attributes"].get("download_count") or 0),
               reverse=True)
    seen, unique = set(), []
    for r in picks:
        name = (r["attributes"].get("release") or "").strip().lower()
        if name and name in seen:
            continue
        seen.add(name)
        unique.append(r)
    return unique[:MAX_CANDIDATES]


async def _download(client: httpx.AsyncClient, auth: dict, candidate: dict) -> bytes | None:
    attrs = candidate["attributes"]
    resp = await _post(client, "download", {"file_id": attrs["files"][0]["file_id"]},
                       headers=auth)
    if resp.status_code == 406:
        raise QuotaExhausted(f"OpenSubtitles download quota is spent: {resp.text[:200]}")
    if resp.status_code in (401, 403):
        _session.update(token="", at=0.0)
    if resp.status_code != 200:
        log.warning("OpenSubtitles download rejected for %s: %s %s",
                    attrs.get("release") or attrs.get("subtitle_id"),
                    resp.status_code, resp.text[:200])
        return None
    answer = resp.json()
    if answer.get("remaining") is not None:
        spent["remaining"] = answer["remaining"]
        if answer["remaining"] <= 0:
            log.info("OpenSubtitles allowance is down to nothing; %s",
                     answer.get("reset_time") or "it renews at midnight UTC")
    link = answer.get("link")
    if not link:
        return None
    try:
        async with httpx.AsyncClient(timeout=45) as fetcher:
            body = await fetcher.get(link)
            body.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("OpenSubtitles file could not be read: %s", exc)
        return None
    return body.content


def _search_params(langs: list[str], *, imdb_id: str | None, tmdb_id: int | None,
                   parent_tmdb_id: int | None, season: int | None,
                   episode: int | None, query: str | None) -> dict:
    params: dict = {"languages": ",".join(sorted(langs))}
    if parent_tmdb_id and season is not None and episode is not None:
        # a series, a season and a number is the only way to ask for one
        # episode; a free-text release title lands on whatever the provider
        # decides it resembles
        params.update({"parent_tmdb_id": parent_tmdb_id, "season_number": season,
                       "episode_number": episode, "type": "episode"})
    elif imdb_id:
        # OpenSubtitles wants the numeric id with no "tt" and no leading zeros
        # (it 301-redirects otherwise, which httpx would not follow)
        digits = imdb_id.lstrip("t")
        params["imdb_id"] = str(int(digits)) if digits.isdigit() else digits
    elif tmdb_id:
        params["tmdb_id"] = tmdb_id
    elif query:
        params["query"] = query
    else:
        raise SubtitleProviderError("no identity to search subtitles by")
    return params


async def _first_fitting(client: httpx.AsyncClient, auth: dict, candidates: list[dict],
                         video: Path, lang: str, duration_s: float | None) -> Path | None:
    target = video.with_name(f"{video.stem}.{lang}.srt")
    # written beside its video under a working name, so a candidate that
    # turns out to belong to another cut never exists as a sidecar the
    # scan would adopt
    part = target.with_name(target.name + ".part")
    for candidate in candidates:
        release = (candidate["attributes"].get("release")
                   or candidate["attributes"].get("subtitle_id"))
        content = await _download(client, auth, candidate)
        if content is None:
            continue
        part.write_bytes(content)
        fit = timing.check(part, duration_s)
        if fit.mismatch:
            log.info("subtitle for %s [%s] discarded, it belongs to another "
                     "release (%s): %s", video.name, lang, release,
                     timing.describe(fit, duration_s))
            part.unlink()
            continue
        os.replace(part, target)
        return target
    log.info("no %s subtitle for %s fits it: %s candidates tried",
             lang, video.name, len(candidates))
    return None


async def fetch(config, *, video_path: str, langs: list[str],
                duration_s: float | None = None,
                imdb_id: str | None = None, tmdb_id: int | None = None,
                parent_tmdb_id: int | None = None, season: int | None = None,
                episode: int | None = None, query: str | None = None) -> list[dict]:
    """Best-effort download of one subtitle per requested language, saved as
    '<video>.<lang>.srt' sidecars. Returns [{path, lang}] for what landed.

    A candidate is taken only if its timings reach the end of THIS file and no
    further — a search answers about the title, and half the copies of a title
    are another cut. One that does not fit is dropped and the next release for
    that language is tried, up to MAX_CANDIDATES.

    Raises SubtitleProviderError on setup/auth problems and when the account's
    download quota is spent; a language simply not existing is an empty result,
    not an error."""
    if not configured(config):
        raise SubtitleProviderError("OpenSubtitles is not configured (key/username/password)")

    video = Path(video_path)
    headers = {"Api-Key": config.get("opensubtitles_api_key"), "User-Agent": APP_UA}
    params = _search_params(langs, imdb_id=imdb_id, tmdb_id=tmdb_id,
                            parent_tmdb_id=parent_tmdb_id, season=season,
                            episode=episode, query=query)

    file_hash = await asyncio.to_thread(_moviehash, str(video))
    if file_hash:
        params["moviehash"] = file_hash

    saved = []
    async with httpx.AsyncClient(timeout=45, base_url=BASE, headers=headers) as client:
        # OpenSubtitles 301-redirects to a canonical form (sorted params, and it
        # also normalizes free-text queries); follow it on the search GET
        resp = await http.get(client, "subtitles", params=sorted(params.items()),
                              follow_redirects=True, attempts=3, pause=2)
        if resp.status_code != 200:
            raise SubtitleProviderError(f"OpenSubtitles search failed: {resp.status_code} {resp.text[:200]}")
        results = resp.json().get("data", [])
        if not results:
            return []

        token = await _login(client, config)
        auth = {"Authorization": f"Bearer {token}"}

        for lang in langs:
            candidates = _candidates(results, lang)
            if candidates:
                landed = await _first_fitting(client, auth, candidates, video, lang, duration_s)
                if landed is not None:
                    saved.append({"path": str(landed), "lang": lang})
    return saved
