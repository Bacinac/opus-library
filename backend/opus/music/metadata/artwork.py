"""Multi-source artwork: gather cover/portrait candidates from Deezer, iTunes,
Spotify and Discogs (Wikimedia candidates arrive via enrichment), score them
and keep the best. Credentialed sources (Spotify, Discogs) are skipped when
unconfigured — a configuration state visible in Settings, not a fallback."""

import logging

import httpx
from rapidfuzz import fuzz
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from opus import http
from opus.db import SessionLocal
from opus.music.metadata import catalog as catalogs
from opus.music.metadata.deezer import DeezerClient
from opus.music.metadata.discogs import DiscogsClient, DiscogsError
from opus.music.metadata.spotify import SpotifyClient, SpotifyCooldown, SpotifyError
from opus.music.metadata import wikidata as wd
from opus.models import Artist, ArtistExternalId, Image, Release
from opus.settings_store import current_runtime
from opus.music.textnorm import norm

log = logging.getLogger("opus.artwork")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
# artist and album are checked separately, never as one blended string: a
# blend lets either half's strong match carry a mismatch in the other half
# past a single threshold. "Pink Floyd The Dark Side of the Moon" blended
# against "Moon Byul Dark Side of the Moon - EP" scored 84 (the shared album
# title carrying a wrong artist, which scores 32 alone); blended against
# "Pink Floyd The Wall" it scored 85 the other way (the shared artist name
# carrying a wrong album, which scores 55 alone). token_set_ratio is used for
# the album half specifically because a legitimate reissue adds words a
# plain ratio would be penalised for ("The Dark Side of the Moon" is a clean
# subset of "The Dark Side of the Moon 50th Anniversary 2023 Remaster").
ITUNES_ARTIST_MATCH_THRESHOLD = 70
ITUNES_ALBUM_MATCH_THRESHOLD = 65
# tie-break when pixel areas are equal or unknown
SOURCE_PRIORITY = {"itunes": 5, "deezer": 4, "spotify": 3, "discogs": 2, "wikimedia": 1, "album": 0}


class ArtworkError(Exception):
    pass


# md5("") — the image-id Deezer serves for artists/albums with no picture
DEEZER_EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


async def _deezer_image_alive(url: str) -> bool:
    """Deezer never 404s a removed picture: the URL 302-redirects to the
    generic placeholder (the md5-of-empty-string segment). A candidate that
    is the placeholder outright, or redirects anywhere, is 'no real image' —
    otherwise it masks the album-cover fallback with a grey avatar."""
    if f"/{DEEZER_EMPTY_MD5}/" in url:
        return False
    async with httpx.AsyncClient(
        timeout=10, headers={"User-Agent": http.USER_AGENT}
    ) as client:
        resp = await client.head(url)
    return resp.status_code == 200


def _score(image: Image) -> tuple[int, int]:
    area = (image.width or 600) * (image.height or 600)
    rank = (catalogs.artwork_rank(image.source) if image.source in catalogs.PRIMARY
            else SOURCE_PRIORITY.get(image.source, 0))
    return (area, rank)


async def fetch_image(url: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers={"User-Agent": http.USER_AGENT}
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "image/jpeg").split(";")[0]


async def _itunes_album_candidates(artist: str, album: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": http.USER_AGENT}) as client:
        resp = await http.get(client, ITUNES_SEARCH_URL, params={
            "term": f"{artist} {album}", "entity": "album", "limit": 5,
        }, attempts=3, pause=2)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    out = []
    for hit in results:
        if fuzz.token_sort_ratio(norm(artist), norm(hit.get("artistName", ""))) < ITUNES_ARTIST_MATCH_THRESHOLD:
            continue
        if fuzz.token_set_ratio(norm(album), norm(hit.get("collectionName", ""))) < ITUNES_ALBUM_MATCH_THRESHOLD:
            continue
        url = hit.get("artworkUrl100")
        if not url:
            continue
        # the CDN serves arbitrary sizes by rewriting the dimension segment
        out.append({
            "url": url.replace("100x100bb", "3000x3000bb"),
            "width": 3000, "height": 3000,
        })
    return out


async def _discogs_release_candidates(token: str, artist: str, album: str) -> list[dict]:
    discogs = DiscogsClient(token)
    try:
        hits = await discogs.search_releases(artist, album)
    finally:
        await discogs.close()
    return [
        {"url": hit["cover_image"], "width": None, "height": None}
        for hit in hits
        if hit.get("cover_image") and not hit["cover_image"].endswith("spacer.gif")
    ][:3]


async def _discogs_artist_images(token: str, discogs_artist_id: str) -> list[dict]:
    discogs = DiscogsClient(token)
    try:
        return (await discogs.get_artist(int(discogs_artist_id)))["images"][:4]
    finally:
        await discogs.close()


async def _store_candidates(session, entity_type: str, entity_id: int,
                            by_source: dict[str, list[dict]]) -> None:
    """Every key in by_source is a source that WAS consulted this refresh —
    its stored candidates are replaced wholesale, so an image the source has
    since removed does not linger. Unconsulted sources (unconfigured Spotify/
    Discogs, no catalogue id) keep their rows. Only the MANUAL flag survives the
    replacement, carried by URL — the same picture coming back is the same
    picture, and losing it silently undoes a deliberate pick. An automatic
    choice is deliberately NOT carried: it is re-decided from the refreshed
    candidates, which is how a better image gets to win."""
    manual_urls = {
        url for url, in await session.execute(
            select(Image.url)
            .where(Image.entity_type == entity_type,
                   Image.entity_id == entity_id,
                   Image.source.in_(list(by_source)),
                   Image.chosen_manual)
        )
    }
    await session.execute(
        delete(Image).where(Image.entity_type == entity_type,
                            Image.entity_id == entity_id,
                            Image.source.in_(list(by_source)))
    )
    for source, candidates in by_source.items():
        for candidate in candidates:
            manual = candidate["url"] in manual_urls
            await session.execute(
                insert(Image)
                .values(entity_type=entity_type, entity_id=entity_id, source=source,
                        url=candidate["url"], width=candidate.get("width"),
                        height=candidate.get("height"),
                        chosen=manual, chosen_manual=manual)
                .on_conflict_do_nothing()
            )


async def _auto_choose(session, entity_type: str, entity_id: int) -> Image | None:
    """Pick the highest-scoring candidate unless one was chosen manually."""
    result = await session.execute(
        select(Image).where(Image.entity_type == entity_type, Image.entity_id == entity_id)
    )
    images = result.scalars().all()
    if not images:
        return None
    manual = next((i for i in images if i.chosen_manual), None)
    if manual is not None:
        return manual  # a deliberate pick is never overridden
    chosen = next((i for i in images if i.chosen), None)
    best = max(images, key=_score)
    if chosen is not None and chosen.id != best.id:
        return chosen  # previous choice stands until re-chosen in the UI
    best.chosen = True
    return best


async def refresh_release_artwork(release_id: int) -> None:
    async with SessionLocal() as session:
        release = await session.get(Release, release_id)
        if release is None:
            raise ArtworkError(f"release {release_id} not found")
        artist = await release.awaitable_attrs.artist
        artist_name, album_title, deezer_id = artist.name, release.title, release.deezer_id
        held = _held_by_catalogues(release)
        config = await current_runtime()

    by_source: dict[str, list[dict]] = {}

    for source, catalog_id in held.items():
        if (found := await _from_catalogue(source, "cover", catalog_id, "release", release_id)) is not None:
            by_source[source] = found

    if deezer_id is not None:
        client = DeezerClient()
        try:
            album = await client.get_album(deezer_id)
        finally:
            await client.close()
        url = album.get("cover_xl")
        try:
            alive = bool(url) and await _deezer_image_alive(url)
        except httpx.HTTPError as exc:
            log.warning("deezer cover check failed for release %s: %s", release_id, exc)
        else:
            by_source["deezer"] = [{"url": url, "width": 1000, "height": 1000}] if alive else []

    by_source["itunes"] = await _itunes_album_candidates(artist_name, album_title)

    if config.get("spotify_client_id") and config.get("spotify_client_secret"):
        spotify = SpotifyClient(config.get("spotify_client_id"),
                                config.get("spotify_client_secret"))
        try:
            images = await spotify.search_album_images(artist_name, album_title)
            by_source["spotify"] = [images[0]] if images else []
        except SpotifyCooldown as exc:
            log.debug("spotify artwork skipped for release %s: %s", release_id, exc)
        except SpotifyError as exc:
            log.error("spotify artwork failed for release %s: %s", release_id, exc)
        finally:
            await spotify.close()

    if config.get("discogs_token"):
        try:
            by_source["discogs"] = await _discogs_release_candidates(
                config.get("discogs_token"), artist_name, album_title
            )
        except DiscogsError as exc:
            log.error("discogs artwork failed for release %s: %s", release_id, exc)

    async with SessionLocal() as session:
        await _store_candidates(session, "release", release_id, by_source)
        chosen = await _auto_choose(session, "release", release_id)
        if chosen is not None:
            await session.execute(
                update(Release).where(Release.id == release_id).values(cover_url=chosen.url)
            )
        await session.commit()


async def refresh_artist_artwork(artist_id: int) -> None:
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        if artist is None:
            raise ArtworkError(f"artist {artist_id} not found")
        deezer_id = artist.deezer_id
        held = _held_by_catalogues(artist)
        wikidata_id = artist.wikidata_id
        external = {
            e.source: e.external_id
            for e in (await session.execute(
                select(ArtistExternalId).where(ArtistExternalId.artist_id == artist_id)
            )).scalars()
        }
        config = await current_runtime()

    answers = {
        **{source: await _from_catalogue(source, "portrait", catalog_id, "artist", artist_id)
           for source, catalog_id in held.items()},
        "deezer": await _deezer_portrait(deezer_id, artist_id),
        "spotify": await _spotify_portrait(config, external.get("spotify"), artist_id),
        "discogs": await _discogs_portraits(config, external.get("discogs"), artist_id),
        "wikimedia": await _wikimedia_portrait(wikidata_id, artist_id),
    }
    by_source = {source: found for source, found in answers.items() if found is not None}
    # last resort: an artist no source pictures (obscure bands the streaming
    # services barely know) borrows one of their own album covers
    by_source["album"] = [] if any(by_source.values()) else await _album_cover_portrait(artist_id)

    async with SessionLocal() as session:
        await _store_candidates(session, "artist", artist_id, by_source)
        chosen = await _auto_choose(session, "artist", artist_id)
        if chosen is not None:
            await session.execute(
                update(Artist).where(Artist.id == artist_id).values(image_url=chosen.url)
            )
        await session.commit()

    log.info("artist %s artwork refreshed (%d sources)", artist_id, len(by_source))


def _held_by_catalogues(row) -> dict[str, int]:
    """The ids the catalogues above Deezer hold this artist or release under."""
    return {source: catalog_id for source in catalogs.PRIMARY
            if (catalog_id := getattr(row, catalogs.id_field(source))) is not None}


async def _from_catalogue(source: str, picture: str, catalog_id: int, entity: str,
                          entity_id: int) -> list[dict] | None:
    catalog = catalogs.open_catalog(source)
    try:
        return await getattr(catalog, picture)(catalog_id)
    except Exception as exc:
        log.error("%s artwork failed for %s %s: %s", source, entity, entity_id, exc)
        return None
    finally:
        await catalog.close()


async def _deezer_portrait(deezer_id: int | None, artist_id: int) -> list[dict] | None:
    if deezer_id is None:
        return None
    client = DeezerClient()
    try:
        data = await client.get_artist(deezer_id)
    finally:
        await client.close()
    url = data.get("picture_xl")
    try:
        alive = bool(url) and await _deezer_image_alive(url)
    except httpx.HTTPError as exc:
        log.warning("deezer picture check failed for artist %s: %s", artist_id, exc)
        return None
    return [{"url": url, "width": 1000, "height": 1000}] if alive else []


async def _spotify_portrait(config, spotify_id: str | None,
                            artist_id: int) -> list[dict] | None:
    if not (spotify_id and config.get("spotify_client_id")
            and config.get("spotify_client_secret")):
        return None
    spotify = SpotifyClient(config.get("spotify_client_id"),
                            config.get("spotify_client_secret"))
    try:
        images = await spotify.artist_images(spotify_id)
        return [images[0]] if images else []
    except SpotifyCooldown as exc:
        log.debug("spotify artwork skipped for artist %s: %s", artist_id, exc)
    except SpotifyError as exc:
        log.error("spotify artwork failed for artist %s: %s", artist_id, exc)
    finally:
        await spotify.close()
    return None


async def _discogs_portraits(config, discogs_id: str | None,
                             artist_id: int) -> list[dict] | None:
    if not (discogs_id and config.get("discogs_token")):
        return None
    try:
        return await _discogs_artist_images(config.get("discogs_token"), discogs_id)
    except DiscogsError as exc:
        log.error("discogs artwork failed for artist %s: %s", artist_id, exc)
        return None


async def _wikimedia_portrait(wikidata_id: str | None, artist_id: int) -> list[dict] | None:
    """Lowest priority: fills the gap for artists the streaming services
    barely know (P18 portrait, else the Wikipedia lead image)."""
    if not wikidata_id:
        return None
    client = wd.WikidataClient()
    try:
        url = await client.artist_image(wikidata_id)
        return [{"url": url, "width": 1000, "height": 1000}] if url else []
    except Exception as exc:
        log.error("wikimedia artwork failed for artist %s: %s", artist_id, exc)
        return None
    finally:
        await client.close()


async def _album_cover_portrait(artist_id: int) -> list[dict]:
    async with SessionLocal() as session:
        cover = (await session.execute(
            select(Release.cover_url)
            .where(Release.artist_id == artist_id, Release.cover_url.isnot(None))
            .order_by(Release.release_date.desc().nullslast())
            .limit(1)
        )).scalar_one_or_none()
    if not cover:
        return []
    # release rows store the 250px thumbnail; the Deezer CDN serves
    # any size by URL segment, so lift it to a proper portrait size
    if "dzcdn.net/images/" in cover:
        cover = cover.replace("/250x250-", "/1000x1000-")
    return [{"url": cover, "width": None, "height": None}]


async def list_candidates(session, entity_type: str, entity_id: int) -> list[dict]:
    result = await session.execute(
        select(Image)
        .where(Image.entity_type == entity_type, Image.entity_id == entity_id)
        .order_by(Image.id)
    )
    return [
        {
            "id": i.id, "source": i.source, "url": i.url,
            "width": i.width, "height": i.height, "chosen": i.chosen,
            "chosen_manual": i.chosen_manual,
        }
        for i in result.scalars()
    ]


async def choose(session, entity_type: str, entity_id: int, image_id: int) -> Image:
    result = await session.execute(
        select(Image).where(Image.id == image_id, Image.entity_type == entity_type,
                            Image.entity_id == entity_id)
    )
    image = result.scalar_one_or_none()
    if image is None:
        raise ArtworkError(f"image {image_id} not found for {entity_type} {entity_id}")
    await session.execute(
        update(Image)
        .where(Image.entity_type == entity_type, Image.entity_id == entity_id)
        .values(chosen=False, chosen_manual=False)
    )
    image.chosen = True
    image.chosen_manual = True
    if entity_type == "release":
        await session.execute(
            update(Release).where(Release.id == entity_id).values(cover_url=image.url)
        )
    else:
        await session.execute(
            update(Artist).where(Artist.id == entity_id).values(image_url=image.url)
        )
    await session.commit()
    return image


async def chosen_url(session, entity_type: str, entity_id: int) -> str | None:
    result = await session.execute(
        select(Image.url).where(Image.entity_type == entity_type,
                                Image.entity_id == entity_id, Image.chosen)
    )
    return result.scalar_one_or_none()
