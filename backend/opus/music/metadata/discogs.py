"""Discogs API client — the catalog source that knows physical (Jugoton-era)
releases absent from streaming. Personal-token auth, 60 requests/min budget
enforced by a module-wide throttle shared across client instances."""

import logging
import re

import httpx
from rapidfuzz import fuzz

from opus import http
from opus.music.textnorm import norm

BASE_URL = "https://api.discogs.com"
MIN_INTERVAL_SECONDS = 1.1  # stays under 60/min
RATE_RETRIES = 3
ARTIST_MATCH_THRESHOLD = 90

log = logging.getLogger("opus.discogs")

_pace = http.Throttle(MIN_INTERVAL_SECONDS)


class DiscogsError(Exception):
    pass


class DiscogsNotFound(DiscogsError):
    pass


def _title(hit: dict) -> str:
    # search titles come as "Artist - Album"
    title = hit.get("title", "")
    return title.split(" - ", 1)[1] if " - " in title else title


class DiscogsClient:
    def __init__(self, token: str):
        self._client = httpx.AsyncClient(
            base_url=BASE_URL, timeout=20,
            headers={"User-Agent": http.USER_AGENT, "Authorization": f"Discogs token={token}"},
        )
        # per-client catalog cache: the scan asks about the same artist for
        # every one of their folders, at 1.1s per request
        self._masters_cache: dict[int, list[dict]] = {}

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, **params) -> dict:
        resp = await http.get(self._client, path, params=params, attempts=RATE_RETRIES,
                              pause=5, throttle=_pace)
        if resp.status_code == 404:
            raise DiscogsNotFound(f"Discogs has nothing at {path}")
        if resp.status_code != 200:
            raise DiscogsError(f"Discogs API error on {path}: {resp.status_code}")
        return resp.json()

    async def find_artist_ids(self, name: str, limit: int = 6) -> list[int]:
        """All plausibly matching artist pages. Discogs disambiguates homonyms
        with a " (N)" suffix ("Azra (3)") — that is metadata, not the name, so
        it is stripped before scoring; the caller validates each candidate
        against known albums."""
        data = await self._get("/database/search", q=name, type="artist", per_page=10)
        out = []
        for hit in data.get("results", []):
            title = re.sub(r"\s*\(\d+\)$", "", hit.get("title", "")).strip()
            if fuzz.token_sort_ratio(norm(name), norm(title)) >= ARTIST_MATCH_THRESHOLD:
                out.append(hit["id"])
        return out[:limit]

    async def find_artist_id(self, name: str) -> int | None:
        ids = await self.find_artist_ids(name, limit=1)
        return ids[0] if ids else None

    async def get_artist(self, artist_id: int) -> dict:
        data = await self._get(f"/artists/{artist_id}")
        # Discogs disambiguates same-named artists with a " (2)" suffix
        name = re.sub(r"\s*\(\d+\)$", "", data.get("name", "")).strip()
        images = [{"url": img["uri"], "width": img.get("width"), "height": img.get("height")}
                  for img in data.get("images", []) if img.get("uri")]
        return {"id": artist_id, "name": name, "images": images}

    async def search_masters(self, artist: str, album: str) -> list[dict]:
        data = await self._get("/database/search", artist=artist,
                               release_title=album, type="master", per_page=5)
        return [{"id": hit["id"], "title": _title(hit), "year": hit.get("year"),
                 "thumb": hit.get("thumb")}
                for hit in data.get("results", [])]

    async def search_releases(self, artist: str, album: str) -> list[dict]:
        """Release-level search — for albums with no master grouping (small /
        regional pressings), which type=master search never surfaces."""
        data = await self._get("/database/search", artist=artist,
                               release_title=album, type="release", per_page=5)
        return [{"id": hit["id"], "title": _title(hit), "year": hit.get("year"),
                 "thumb": hit.get("thumb"), "cover_image": hit.get("cover_image")}
                for hit in data.get("results", [])]

    async def artist_main_masters(self, artist_id: int) -> list[dict]:
        """Main-role masters only — editions collapse onto their master, which
        keeps 50-pressings-per-album noise out of the catalog."""
        cached = self._masters_cache.get(artist_id)
        if cached is not None:
            return cached
        out: list[dict] = []
        page = 1
        # mega-artists list 40+ pages of pressings; 30 pages (3000 releases)
        # covers every real discography
        while page <= 30:
            data = await self._get(
                f"/artists/{artist_id}/releases", sort="year", per_page=100, page=page
            )
            for row in data.get("releases", []):
                if row.get("type") == "master" and row.get("role") == "Main":
                    out.append({
                        "id": row["id"],
                        "title": row["title"],
                        "year": row.get("year"),
                        "thumb": row.get("thumb"),
                    })
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            page += 1
        self._masters_cache[artist_id] = out
        return out

    async def cached_artist_masters(self, session, artist_id: int) -> list[dict]:
        """artist_main_masters through the persistent per-week Postgres cache —
        the full listing walk is too expensive to repeat per process."""
        from datetime import datetime, timedelta, timezone

        from opus.models import DiscogsMastersCache

        row = await session.get(DiscogsMastersCache, artist_id)
        if row is not None:
            if row.fetched_at > datetime.now(timezone.utc) - timedelta(days=7):
                self._masters_cache[artist_id] = row.masters
                return row.masters
            await session.delete(row)
            await session.flush()
        try:
            masters = await self.artist_main_masters(artist_id)
        except DiscogsNotFound as exc:
            log.warning("discogs has no release listing for artist %s, not asked again "
                        "for a week: %s", artist_id, exc)
            masters = []
        session.add(DiscogsMastersCache(artist_id=artist_id, masters=masters))
        await session.flush()
        return masters

    async def store_masters(self, session, artist_id: int, masters: list[dict]):
        """Write enriched entries (typed record_type etc.) back into the
        persistent cache so downstream passes never repeat the work."""
        from opus.models import DiscogsMastersCache

        row = await session.get(DiscogsMastersCache, artist_id)
        if row is None:
            session.add(DiscogsMastersCache(artist_id=artist_id, masters=masters))
        else:
            row.masters = masters
        self._masters_cache[artist_id] = masters
        await session.flush()

    async def master_details(self, master_id: int) -> dict:
        """Original year + track count — enough to type an untyped master
        (Discogs artist listings carry no format for masters)."""
        data = await self._get(f"/masters/{master_id}")
        tracks = [r for r in data.get("tracklist", []) if r.get("type_", "track") == "track"]
        return {"year": data.get("year"), "track_count": len(tracks)}

    @staticmethod
    def _parse_tracklist(data: dict) -> list[dict]:
        tracks: list[dict] = []
        position = 0
        for row in data.get("tracklist", []):
            if row.get("type_", "track") != "track":
                continue
            position += 1
            duration = None
            raw = row.get("duration") or ""
            if ":" in raw:
                try:
                    minutes, seconds = raw.split(":")[-2:]
                    duration = int(minutes) * 60 + int(seconds)
                except ValueError:
                    duration = None
            tracks.append({"position": position, "title": row["title"], "duration_sec": duration})
        return tracks

    async def master_tracklist(self, master_id: int) -> list[dict]:
        return self._parse_tracklist(await self._get(f"/masters/{master_id}"))

    async def master_versions(self, master_id: int) -> list[dict]:
        """Editions under a master. The master tracklist is only the main
        release's; reissues (2CD boxes etc.) carry their own, often longer,
        lists that exist nowhere else."""
        out: list[dict] = []
        # the reliable format field here is major_formats (a list); "format"
        # carries only descriptions like "Album, Reissue"
        for page in (1, 2, 3):
            data = await self._get(f"/masters/{master_id}/versions",
                                   per_page=50, page=page)
            out.extend(
                {"id": v["id"], "title": v.get("title", ""),
                 "format": ", ".join(
                     [*(v.get("major_formats") or []), v.get("format") or ""]),
                 "year": v.get("released")}
                for v in data.get("versions", [])
            )
            if page >= data.get("pagination", {}).get("pages", 1):
                break
        return out

    async def release_tracklist(self, release_id: int) -> list[dict]:
        return self._parse_tracklist(await self._get(f"/releases/{release_id}"))
