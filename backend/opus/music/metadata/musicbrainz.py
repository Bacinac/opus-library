"""MusicBrainz — edition EVIDENCE only, never catalog identity. The music
metadata backbone stays Deezer/Discogs/Spotify/Wikidata (the anti-Lidarr
premise); MusicBrainz answers the one narrow question the others answer
poorly: does an official edition with exactly THESE tracks exist?"""

import httpx

from opus import http
from opus.music.textnorm import album_score, same_artist

_BASE = "https://musicbrainz.org/ws/2"

# MusicBrainz etiquette: one request per second, identifying User-Agent
_pace = http.Throttle(1.1)


class MusicBrainzError(Exception):
    pass


def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


class MusicBrainzClient:
    async def _get(self, path: str, **params) -> dict:
        try:
            async with httpx.AsyncClient(
                timeout=20, headers={"User-Agent": http.USER_AGENT}
            ) as client:
                resp = await http.get(client, f"{_BASE}{path}",
                                      params={**params, "fmt": "json"},
                                      attempts=4, pause=2, throttle=_pace)
        except httpx.HTTPError as exc:
            raise MusicBrainzError(f"request failed: {exc!r}") from exc
        if resp.status_code != 200:
            raise MusicBrainzError(f"HTTP {resp.status_code} on {path}")
        return resp.json()

    # what MusicBrainz calls a person standing in a group. `founder` is one of
    # them and is how it records a project named after somebody — `Mile i
    # Putnici` names Mile Kekin that way and nothing else records it at all.
    LINE_UP = ("member of band", "founder", "collaboration")

    async def find_artist(self, name: str) -> str | None:
        """This artist's MusicBrainz id, found by name.

        Wanted for the ones nothing else knows: a Wikidata entry usually
        carries the id as a claim, and an artist with no Wikidata entry at all
        has no claim to carry it. `Mile i Putnici` is one — nowhere in Wikidata,
        an exact match here.

        Exact name and a top score, or nothing. A near-miss on a name is how a
        Croatian trio becomes a Swedish one."""
        data = await self._get("/artist", query=name, limit=5)
        for found in data.get("artists") or []:
            if found.get("score", 0) >= 90 and same_artist(found.get("name", ""), name):
                return found.get("id")
        return None

    async def artist_relations(self, mbid: str) -> list[dict]:
        """Who was in a group, as MusicBrainz has it.

        Membership is a first-class relation here, with the years attached,
        which is why it carries line-ups the other sources do not: Wikidata
        knows five of Black Sabbath's and this knows forty-six. It says nothing
        about the catalogue — the records still come from Deezer — and the
        premise this module was written under is untouched.

        The direction matters: the same relation hangs off both ends, so the
        one that is a person is the member and the other is the group."""
        data = await self._get(f"/artist/{mbid}", inc="artist-rels")
        found = []
        for rel in data.get("relations") or []:
            if rel.get("type") not in self.LINE_UP:
                continue
            other = rel.get("artist") or {}
            if not other.get("id") or not other.get("name"):
                continue
            found.append({
                "mbid": other["id"],
                "name": other["name"],
                "person": other.get("type") == "Person",
                "since": rel.get("begin"),
                "until": rel.get("end"),
            })
        return found

    async def release_editions(self, artist: str, album: str) -> list[dict]:
        """Every edition (release) of an album with its track count. Search
        only finds the release group; browsing the group returns ALL its
        editions, which plain release search misses."""
        data = await self._get(
            "/release-group",
            query=f"releasegroup:{_quote(album)} AND artist:{_quote(artist)}",
            limit=5,
        )
        group = next(
            (g for g in data.get("release-groups", [])
             if album_score(g.get("title", ""), album) >= 85),
            None,
        )
        if group is None:
            return []
        rel = await self._get(
            "/release", **{"release-group": group["id"], "inc": "media", "limit": 100}
        )
        return [
            {
                "id": r["id"],
                "title": r.get("title", ""),
                "track_count": sum(
                    m.get("track-count") or 0 for m in r.get("media", [])
                ),
                "date": r.get("date"),
                "country": r.get("country"),
            }
            for r in rel.get("releases", [])
        ]

    async def release_titles(self, release_id: str) -> list[str]:
        """Ordered track titles of one edition, across all its media."""
        data = await self._get(f"/release/{release_id}", inc="recordings")
        titles: list[str] = []
        for medium in sorted(data.get("media", []),
                             key=lambda m: m.get("position") or 0):
            for track in sorted(medium.get("tracks", []),
                                key=lambda t: t.get("position") or 0):
                titles.append(track.get("title", ""))
        return titles
