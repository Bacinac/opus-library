"""Tracklist materialisation: the tracks a release still lacks, fetched on
demand from whichever source knows it (the catalogues, Discogs, Spotify — in
that order), plus the album description from its Wikipedia article. Nothing
here decides identity — it fills in a release the sync already built, and
fails loud when no configured source can. Owns DiscographyError, the failure
both this fill-in and the sync raise."""

import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from opus.music.metadata import catalog as catalogs
from opus.music.metadata import wikidata as wd
from opus.music.metadata.deezer import DeezerClient
from opus.music.metadata.discogs import DiscogsClient
from opus.music.metadata.spotify import SpotifyClient
from opus.models import TRACK_CATALOG_IDS, Release, Track
from opus.settings_store import current_runtime

log = logging.getLogger("opus.tracklists")


class DiscographyError(Exception):
    pass


_QUALIFIER = re.compile(r"\s*[\(\[][^)\]]+[\)\]]\s*$")


def strip_edition_qualifier(titles: list[str]) -> list[str]:
    """A parenthetical suffix shared by (nearly) all tracks of a release —
    "(Live Unplugged)" on every track — is edition noise, not part of the song
    name. Strip it so catalog titles read like Wikipedia's, and so the tagger
    writes clean tags."""
    if len(titles) < 3:
        return titles
    suffixes = []
    for title in titles:
        match = _QUALIFIER.search(title)
        suffixes.append(match.group(0).strip().lower() if match else None)
    counts = Counter(s for s in suffixes if s)
    if not counts:
        return titles
    common, occurrences = counts.most_common(1)[0]
    if occurrences < 0.8 * len(titles):
        return titles
    return [
        _QUALIFIER.sub("", title).strip() if suffix == common else title
        for title, suffix in zip(titles, suffixes)
    ]


async def ensure_description(release: Release, client=None):
    """What a record is, from its own Wikipedia article; hr for Croatian
    artists, en otherwise.

    Two ways in. The Wikidata entity, when the catalogue has one, names the
    article outright through its sitelink — that is the certain path and it is
    tried first. Without an entity the article is looked for by name, which is
    the uncertain one and is why the search insists the title be the record's
    and the text name the artist.

    An empty description means asked and there was nothing, so a record with no
    article anywhere is not a request every time somebody opens it. `client`
    lets a sweep over the whole shelf share one.
    """
    if release.description is not None:
        return
    artist = await release.awaitable_attrs.artist
    langs = ("hr", "en") if artist.country == "Croatia" else ("en", "hr")
    mine = client is None
    client = client or wd.WikidataClient()
    try:
        sitelinks = {}
        if release.wikidata_id:
            entities = await client.get_entities([release.wikidata_id])
            sitelinks = (entities.get(release.wikidata_id) or {}).get("sitelinks", {})
        found = ""
        for lang in langs:
            sitelink = sitelinks.get(f"{lang}wiki")
            title = sitelink["title"] if sitelink else await client.wikipedia_album_title(
                lang, artist.name, release.title
            )
            if not title:
                continue
            article = await client.wikipedia_extract(lang, title)
            if article:
                found = article["extract"]
                break
        release.description = found
    finally:
        if mine:
            await client.close()


async def ensure_facts(release: Release, deezer: DeezerClient | None = None):
    """Who put the record out and what kind of music it is.

    Deezer knows this for nearly every record here. The genres are kept under
    Deezer's English names and each reader's interface says them in its own
    language. It is one request per record and it is asked once: an empty
    label means asked and there was nothing.
    """
    if release.label is not None or release.deezer_id is None:
        return
    mine = deezer is None
    deezer = deezer or DeezerClient()
    try:
        album = await deezer.get_album(release.deezer_id)
    finally:
        if mine:
            await deezer.close()
    release.label = (album.get("label") or "").strip()[:200]
    release.genres = [
        g["name"] for g in ((album.get("genres") or {}).get("data") or []) if g.get("name")
    ]


async def _from_catalog(source: str, catalog_id: int) -> list[dict]:
    catalog = catalogs.open_catalog(source)
    try:
        return [
            {
                "position": t.get("track_position", 0),
                "title": t["title"],
                "duration_sec": t.get("duration"),
                catalogs.id_field(source): t["id"],
            }
            for t in await catalog.get_album_tracks(catalog_id)
        ]
    finally:
        await catalog.close()


async def _from_deezer(deezer_id: int) -> list[dict]:
    client = DeezerClient()
    try:
        return [
            {
                "position": t.get("track_position", 0),
                "title": t["title"],
                "duration_sec": t.get("duration"),
                "deezer_id": t["id"],
            }
            for t in await client.get_album_tracks(deezer_id)
        ]
    finally:
        await client.close()


async def _from_discogs(token: str, master_id: int) -> list[dict]:
    discogs = DiscogsClient(token)
    try:
        return await discogs.master_tracklist(master_id)
    finally:
        await discogs.close()


async def _from_spotify(client_id: str, secret: str, album_id: str) -> list[dict]:
    spotify = SpotifyClient(client_id, secret)
    try:
        return await spotify.album_tracks(album_id)
    finally:
        await spotify.close()


def _sources(release: Release, config) -> list[tuple[str, Callable[[], Awaitable[list[dict]]]]]:
    """Every source that could know this release, in the order they are asked."""
    sources = []
    for source in catalogs.PRIMARY:
        if (catalog_id := getattr(release, catalogs.id_field(source))) is not None:
            sources.append((source, lambda source=source, catalog_id=catalog_id:
                            _from_catalog(source, catalog_id)))
    if release.deezer_id is not None:
        sources.append(("deezer", lambda: _from_deezer(release.deezer_id)))
    if release.discogs_id is not None and config.get("discogs_token"):
        sources.append(("discogs", lambda: _from_discogs(
            config.get("discogs_token"), release.discogs_id)))
    if (release.spotify_id and config.get("spotify_client_id")
            and config.get("spotify_client_secret")):
        sources.append(("spotify", lambda: _from_spotify(
            config.get("spotify_client_id"), config.get("spotify_client_secret"),
            release.spotify_id)))
    return sources


async def ensure_tracks(session, release: Release) -> list[Track]:
    """Return the release's tracks, fetching the tracklist from the first source
    that yields one. Fails loud, with every source's answer, when none does."""
    tracks = list((await session.execute(
        select(Track).where(Track.release_id == release.id).order_by(Track.position)
    )).scalars())
    if tracks:
        return tracks

    config = await current_runtime()
    rows: list[dict] = []
    tried: list[str] = []
    for source, fetch in _sources(release, config):
        try:
            rows = await fetch()
        except Exception as exc:
            log.warning("%s tracklist of release %s could not be read: %s",
                        source, release.id, exc)
            tried.append(f"{source}: {exc}")
            continue
        if rows:
            break
        tried.append(f"{source}: no tracks")

    if not rows:
        raise DiscographyError(
            f"no configured source provides a tracklist for release {release.id} "
            f"({release.title})" + (f" — {'; '.join(tried)}" if tried else "")
        )

    for row, title in zip(rows, strip_edition_qualifier([r["title"] for r in rows])):
        row["title"] = title

    tracks = [
        Track(
            release_id=release.id,
            position=row.get("position") or index + 1,
            title=row["title"],
            duration_sec=row.get("duration_sec"),
            **{column: row.get(column) for column in TRACK_CATALOG_IDS},
        )
        for index, row in enumerate(rows)
    ]
    session.add_all(tracks)
    release.track_count = len(tracks)
    await session.flush()
    return tracks
