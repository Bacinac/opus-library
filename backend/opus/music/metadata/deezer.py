"""Deezer API client — canonical metadata source. No API key required.
Rate limit: 50 requests per 5 seconds per IP; on quota errors (code 4) we back
off and retry instead of failing the whole operation."""

import logging

import httpx

from opus import http
from opus.music.textnorm import same_artist

BASE_URL = "https://api.deezer.com"
QUOTA_ERROR_CODE = 4
NO_DATA_ERROR_CODE = 800
QUOTA_RETRIES = 5
QUOTA_BACKOFF_SECONDS = 5

log = logging.getLogger("opus.deezer")

_pace = http.Throttle(0.1)


class DeezerError(Exception):
    pass


class DeezerNotFound(DeezerError):
    pass


def _error_code(resp: httpx.Response) -> int | None:
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def _busy(resp: httpx.Response) -> bool:
    return http.overloaded(resp) or _error_code(resp) == QUOTA_ERROR_CODE


async def verified_claim_id(client: "DeezerClient", claim,
                            expected_name: str) -> int | None:
    """A Wikidata P2722 claim is evidence only when the page it points at
    carries the artist's name. Wikidata holds wrong ids — the Spanish band
    'Animal' (Q115683411) claims Deezer 3350, which is The Animals — and an
    unverified claim hands one act the other's ENTIRE catalog, after which
    every downstream match looks corroborated."""
    if claim is None:
        return None
    try:
        deezer_id = int(claim)
    except (TypeError, ValueError):
        return None
    try:
        page = await client.get_artist(deezer_id)
    except (DeezerError, httpx.HTTPError) as exc:
        log.error("deezer claim %s could not be verified for %r: %s",
                  deezer_id, expected_name, exc)
        return None
    if not same_artist(page.get("name") or "", expected_name):
        log.warning("wikidata deezer claim %s rejected for %r — the page is %r",
                    deezer_id, expected_name, page.get("name"))
        return None
    return deezer_id


class DeezerClient:
    def __init__(self):
        # Deezer names genres in the language of the country asking; from here
        # that was Croatian, a word the English interface then showed as is.
        self._client = httpx.AsyncClient(
            base_url=BASE_URL, timeout=15, headers={"Accept-Language": "en"})
        self._albums_cache: dict[int, list[dict]] = {}

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, **params) -> dict:
        # params or None: httpx wipes the URL's own query string when an
        # (even empty) params dict is passed — that turned every followed
        # "next" link back into page 1 and poisoned pagination
        resp = await http.get(self._client, path, params=params or None,
                              attempts=QUOTA_RETRIES, pause=QUOTA_BACKOFF_SECONDS,
                              throttle=_pace, busy=_busy)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "error" in data:
            code = _error_code(resp)
            if code == QUOTA_ERROR_CODE:
                raise DeezerError(f"Deezer quota still exceeded after {QUOTA_RETRIES} attempts on {path}")
            if code == NO_DATA_ERROR_CODE:
                raise DeezerNotFound(f"Deezer has no data on {path}")
            raise DeezerError(f"Deezer API error on {path}: {data['error']}")
        return data

    async def search_artists(self, query: str) -> list[dict]:
        data = await self._get("/search/artist", q=query)
        return data.get("data", [])

    async def search_albums(self, query: str) -> list[dict]:
        data = await self._get("/search/album", q=query)
        return data.get("data", [])

    async def search_tracks(self, query: str) -> list[dict]:
        data = await self._get("/search/track", q=query)
        return data.get("data", [])

    async def get_artist(self, deezer_id: int) -> dict:
        return await self._get(f"/artist/{deezer_id}")

    async def get_artist_top_tracks(self, deezer_id: int, limit: int = 3) -> list[dict]:
        data = await self._get(f"/artist/{deezer_id}/top", limit=limit)
        return data.get("data", [])

    async def _paginate(self, url: str, what: str) -> list[dict]:
        """Follow Deezer's "next" links with two safety rails: the next URL can
        point back at an already-fetched page (seen on artist 2059 — an
        unguarded loop grows until the process is OOM-killed), and pathological
        entities (compilation hubs) carry tens of thousands of rows."""
        rows: list[dict] = []
        params = {"limit": 100}
        seen: set[str] = set()
        max_pages = 20
        while url:
            if url in seen or len(seen) >= max_pages:
                log.warning("Deezer pagination stopped for %s at %s (%d pages, loop=%s)",
                            what, url, len(seen), url in seen)
                break
            seen.add(url)
            data = await self._get(url, **params)
            rows.extend(data.get("data", []))
            next_url = data.get("next")
            if not next_url:
                break
            # "next" is absolute; strip the base so the client re-applies it
            url = next_url.removeprefix(BASE_URL)
            params = {}
        # belt and braces: never return duplicate rows even if the source
        # re-serves overlapping pages
        unique: dict = {}
        for row in rows:
            unique.setdefault(row.get("id"), row)
        return list(unique.values())

    async def get_artist_albums(self, deezer_id: int) -> list[dict]:
        cached = self._albums_cache.get(deezer_id)
        if cached is not None:
            return cached
        albums = await self._paginate(
            f"/artist/{deezer_id}/albums", f"artist {deezer_id} albums"
        )
        self._albums_cache[deezer_id] = albums
        return albums

    async def get_album(self, deezer_id: int) -> dict:
        return await self._get(f"/album/{deezer_id}")

    async def get_album_tracks(self, deezer_id: int) -> list[dict]:
        """Unlike the album payload's embedded track list, this endpoint
        includes track_position and disk_number."""
        return await self._paginate(f"/album/{deezer_id}/tracks", f"album {deezer_id} tracks")
