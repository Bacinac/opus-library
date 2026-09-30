"""Spotify Web API client (client-credentials flow). Secondary metadata and
artwork source; used only when credentials are configured in Settings."""

import logging
import time

import httpx

from opus import http

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
# a 429 Retry-After above this goes into a module cooldown instead of a sleep
# (Spotify hands dev-mode apps multi-hour Retry-Afters — obeying one inline
# would hang whatever pipeline made the call)
MAX_RETRY_AFTER_SECONDS = 30

log = logging.getLogger("opus.spotify")

_cooldown_until = 0.0


class SpotifyError(Exception):
    pass


class SpotifyCooldown(SpotifyError):
    pass


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = httpx.AsyncClient(timeout=15)
        self._token: str | None = None
        self._token_expires = 0.0
        # per-client catalog cache: the scan asks about the same artist for
        # every one of their folders
        self._albums_cache: dict[str, list[dict]] = {}

    async def close(self):
        await self._client.aclose()

    async def _ensure_token(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires:
            resp = await self._client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
            )
            if resp.status_code != 200:
                raise SpotifyError(f"token request failed: {resp.status_code} {resp.text}")
            data = resp.json()
            self._token = data["access_token"]
            self._token_expires = time.monotonic() + data.get("expires_in", 3600) - 60
        return self._token

    async def _get(self, path: str, **params) -> dict:
        global _cooldown_until
        remaining = _cooldown_until - time.monotonic()
        if remaining > 0:
            raise SpotifyCooldown(f"in rate-limit cooldown for another {remaining:.0f}s")
        token = await self._ensure_token()
        url = path if path.startswith("http") else f"{API_URL}{path}"
        resp = await http.get(self._client, url, params=params or None,
                              headers={"Authorization": f"Bearer {token}"},
                              attempts=3, pause=2, longest=MAX_RETRY_AFTER_SECONDS)
        if resp.status_code == 429:
            wait = http.retry_after(resp) or 0
            if wait > MAX_RETRY_AFTER_SECONDS:
                if _cooldown_until <= time.monotonic():
                    log.warning("spotify rate limit: Retry-After %.0fs, not asked again "
                                "until then", wait)
                _cooldown_until = max(_cooldown_until, time.monotonic() + wait)
                raise SpotifyCooldown(f"rate limited, Retry-After {wait:.0f}s — cooling down")
            raise SpotifyError(f"Spotify rate limit persists on {path}")
        if resp.status_code != 200:
            raise SpotifyError(f"Spotify API error on {path}: {resp.status_code}")
        return resp.json()

    # development-mode apps reject an explicit limit/offset on paged endpoints
    # ("Invalid limit"); pagination follows the server-generated `next` URL

    def _artist_dict(self, a: dict) -> dict:
        images = a.get("images") or []
        image = (images[1] if len(images) > 1 else images[0])["url"] if images else None
        return {
            "id": a["id"],
            "name": a["name"],
            "image_url": image,
            "nb_fan": (a.get("followers") or {}).get("total"),
            "link": (a.get("external_urls") or {}).get("spotify"),
        }

    async def get_artist(self, spotify_artist_id: str) -> dict:
        return self._artist_dict(await self._get(f"/artists/{spotify_artist_id}"))

    async def search_artists(self, query: str) -> list[dict]:
        data = await self._get("/search", q=query, type="artist")
        items = (data.get("artists") or {}).get("items") or []
        return [self._artist_dict(a) for a in items]

    async def artist_top_tracks(self, spotify_artist_id: str,
                                market: str = "HR") -> list[str]:
        data = await self._get(f"/artists/{spotify_artist_id}/top-tracks",
                               market=market)
        return [t["name"] for t in (data.get("tracks") or [])[:3]]

    async def artist_albums(self, spotify_artist_id: str) -> list[dict]:
        cached = self._albums_cache.get(spotify_artist_id)
        if cached is not None:
            return cached
        albums: list[dict] = []
        data = await self._get(f"/artists/{spotify_artist_id}/albums",
                               include_groups="album,compilation")
        while True:
            for item in data.get("items", []):
                images = item.get("images", [])
                albums.append({
                    "id": item["id"],
                    "title": item["name"],
                    "release_date": item.get("release_date"),
                    "record_type": item.get("album_type"),
                    "cover_url": images[0]["url"] if images else None,
                })
            if data.get("next") is None:
                break
            data = await self._get(data["next"])
        self._albums_cache[spotify_artist_id] = albums
        return albums

    async def album_tracks(self, album_id: str) -> list[dict]:
        tracks: list[dict] = []
        data = await self._get(f"/albums/{album_id}/tracks")
        while True:
            for item in data.get("items", []):
                duration_ms = item.get("duration_ms")
                tracks.append({
                    "position": len(tracks) + 1,
                    "title": item["name"],
                    "duration_sec": round(duration_ms / 1000) if duration_ms else None,
                })
            if data.get("next") is None:
                break
            data = await self._get(data["next"])
        return tracks

    async def search_albums(self, artist: str, album: str) -> list[dict]:
        """Album search hits: [{id, title, release_date, record_type, cover_url}]."""
        data = await self._get("/search", q=f"album:{album} artist:{artist}", type="album")
        out = []
        for item in (data.get("albums") or {}).get("items", []):
            images = item.get("images", [])
            out.append({
                "id": item["id"],
                "title": item["name"],
                "release_date": item.get("release_date"),
                "record_type": item.get("album_type"),
                "cover_url": images[0]["url"] if images else None,
            })
        return out

    async def search_album_images(self, artist: str, album: str) -> list[dict]:
        """Returns [{url, width, height}] for the best-matching album."""
        data = await self._get(
            "/search", q=f"album:{album} artist:{artist}", type="album", limit=5
        )
        items = data.get("albums", {}).get("items", [])
        return items[0].get("images", []) if items else []

    async def artist_images(self, spotify_artist_id: str) -> list[dict]:
        data = await self._get(f"/artists/{spotify_artist_id}")
        return data.get("images", [])
