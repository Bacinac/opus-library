"""Album-search fallback: the hunt that runs when the artist's existing
editions do not explain the folder. Every source is exhausted in turn — the
catalog's album search, its track search, Discogs (masters, their versions,
bare releases) and Spotify — each candidate's tracklist scored against the
files on disk, and only a winner that links MORE files than the current best
becomes a release row. Everything else here serves that one decision: query
shaping, the artist's identity in the other catalogs, and the row surgery an
adopted winner forces."""

import logging

from rapidfuzz import fuzz
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from opus.music.library import artists, matching
from opus.music.library.artists import ALBUM_MATCH_THRESHOLD
from opus.music.metadata import catalog as catalogs
from opus.music.metadata import variants
from opus.music.metadata import wikidata as wd
from opus.music.metadata.authority import get_wiki_info
from opus.music.metadata.catalog import CatalogClient
from opus.music.metadata.discogs import DiscogsClient, DiscogsError
from opus.music.metadata.musicbrainz import MusicBrainzClient
from opus.music.metadata.spotify import SpotifyClient, SpotifyCooldown, SpotifyError
from opus.models import (
    RELEASE_SOURCE_IDS,
    Artist,
    ArtistExternalId,
    MusicDownload,
    MusicFile,
    Release,
    ReleaseStatus,
    Track,
)
from opus.music.textnorm import album_score, norm

log = logging.getLogger("opus.libimport")

mb = MusicBrainzClient()


async def _adopt_album_hit(session, artist: Artist, album_id: int, title: str,
                           record_type: str | None, cover_url: str | None,
                           release_date: str | None = None,
                           source: str = "deezer") -> Release | None:
    id_field = catalogs.id_field(source)
    existing = (await session.execute(
        select(Release).where(getattr(Release, id_field) == album_id)
    )).scalar_one_or_none()
    if existing is not None:
        return existing if existing.artist_id == artist.id else None
    release = Release(
        artist_id=artist.id,
        title=title,
        record_type=record_type,
        release_date=release_date,
        cover_url=cover_url,
        **{id_field: album_id},
    )
    session.add(release)
    await session.flush()
    return release


def _query_variants(tag_artist: str, tag_album: str) -> list[str]:
    """Several query shapes before giving up: Deezer's search is literal about
    punctuation ('J.J. Cale' vs its page 'JJ Cale')."""
    compact_artist = tag_artist.replace(".", "").replace("'", "")
    plain_album = tag_album.split(":", 1)[0]
    shapes = [
        f"{tag_artist} {tag_album}",
        f"{compact_artist} {tag_album}",
        f"{compact_artist} {plain_album}",
        tag_album,
    ]
    seen: set[str] = set()
    return [v for v in shapes if not (v in seen or seen.add(v))]


async def resolve_external_ids(session, artist: Artist) -> dict[str, str]:
    """The artist's identity in the OTHER catalogs, by ID — via Wikidata's
    cross-references (P1953 Discogs, P1902 Spotify) — so fallback searches
    the artist's own catalog there instead of guessing by name. Resolved once
    and persisted."""
    rows = (await session.execute(
        select(ArtistExternalId).where(ArtistExternalId.artist_id == artist.id)
    )).scalars().all()
    external = {r.source: r.external_id for r in rows}
    if external or artist.deezer_id is None:
        return external

    client = wd.WikidataClient()
    try:
        qid = artist.wikidata_id or await client.qid_by_deezer_id(artist.deezer_id)
        if qid is None:
            return {}
        entity = (await client.get_entities([qid])).get(qid) or {}
        for prop, source in wd.EXTERNAL_ID_PROPS.items():
            if source == "deezer":
                continue
            value = wd.first_claim(entity, prop)
            if value is not None:
                external[source] = str(value)
                await session.execute(
                    insert(ArtistExternalId)
                    .values(artist_id=artist.id, source=source, external_id=str(value))
                    .on_conflict_do_nothing()
                )
        if artist.wikidata_id is None:
            taken = (await session.execute(
                select(Artist.id).where(Artist.wikidata_id == qid)
            )).scalar_one_or_none()
            if taken is None:
                artist.wikidata_id = qid
        await session.commit()
    except Exception as exc:
        log.error("external-id resolution failed for %s: %s", artist.name, exc)
    finally:
        await client.close()
    return external


async def wikipedia_titles(session, qid: str) -> list[str] | None:
    client = wd.WikidataClient()
    try:
        return (await get_wiki_info(session, client, qid))["tracklist"]
    finally:
        await client.close()


def _version_rank(v: dict):
    """Order a master's versions for tracklist evaluation: CD/File editions
    first (multi-disc rips are almost always CDs, and later reissues carry the
    fuller listings), newest first."""
    fmt = (v.get("format") or "").upper()
    year = str(v.get("year") or "")
    return ("CD" not in fmt and "FILE" not in fmt,
            -(int(year[:4]) if year[:4].isdigit() else 0))


def _version_year(v: dict) -> int:
    year = str(v.get("year") or "")
    value = int(year[:4]) if year[:4].isdigit() else 0
    return value if value > 0 else 9999


def version_candidates(versions: list[dict]) -> list[dict]:
    """The edition we hold sits at one of two ends: a LATER fuller reissue
    (2CD with bonus material) or the EARLIEST original pressing (first-press
    tracklists that later represses replaced — Buffalo Springfield's 'Baby
    Don't Scold Me'). Sample both ends instead of one sort order."""
    newest_cd_first = sorted(versions, key=_version_rank)
    oldest_first = sorted(versions, key=_version_year)
    out: list[dict] = []
    seen: set[int] = set()
    for version in newest_cd_first[:5] + oldest_first[:3]:
        if version["id"] not in seen:
            seen.add(version["id"])
            out.append(version)
    return out


async def _drop_if_husk(session, release: Release) -> bool:
    """A row that just lost its last source id, holds no files and nothing
    waits on it is not a release any more — delete it instead of stranding an
    empty husk the next pass forks around."""
    if any(getattr(release, column) for column in RELEASE_SOURCE_IDS):
        return False
    if release.status != ReleaseStatus.NONE:
        return False
    with session.no_autoflush:
        holds = (await session.execute(
            select(MusicFile.id).join(Track, Track.id == MusicFile.track_id)
            .where(Track.release_id == release.id).limit(1)
        )).first() or (await session.execute(
            select(MusicDownload.id).where(MusicDownload.release_id == release.id).limit(1)
        )).first()
    if holds:
        return False
    await session.delete(release)
    return True


def _materialize_edition(session, release: Release, spec: dict):
    """Create the Track rows of a version-specific Discogs edition directly —
    ensure_tracks would refetch the master and land on the original listing."""
    for t in spec["tracks"]:
        session.add(Track(release_id=release.id, position=t["position"],
                          title=t["title"], duration_sec=t["duration_sec"]))
    release.track_count = spec["total"]


class _Hunt:
    """The best candidate the sources have offered so far, and whether it already
    explains the whole folder."""

    def __init__(self, file_count: int, best_so_far: int):
        self.file_count = file_count
        self.best_so_far = best_so_far
        self.best: dict | None = None
        self.perfect = False
        self.seen_album: set[int] = set()
        self.noticed: list[variants.Sighting] = []

    def consider(self, matched: int, total: int, spec: dict, name_ok: bool = True) -> bool:
        """Keep the candidate if it beats the best; True when it is perfect."""
        file_count = self.file_count
        if matched == 0 or total == 0:
            return False
        if total >= 20 and total > 3 * file_count:
            return False  # box sets never win
        if matched < min(file_count, max(2, file_count // 3)):
            # the candidate must EXPLAIN the folder: a 2-track Discogs release
            # linking one of six files is a fragment, not the album, and the
            # edition it materializes strands the other five
            return False
        if not name_ok and not (matched == total == file_count):
            # a differently-named album (track-search candidates carry no
            # name filter) may only win as a PERFECT fit — jazz-era songs
            # recur across dozens of compilations and a partial hit against
            # a foreign compilation must never swallow the folder
            return False
        complete = matched == total
        # improvement = linking MORE files, or the same files but green — a
        # complete-but-smaller edition never beats one that links more files
        if matched < self.best_so_far or (matched == self.best_so_far and not complete):
            return False
        key = (matched, complete, -total)
        best = self.best
        if best is None or key > (best["matched"], best["matched"] == best["total"],
                                  -best["total"]):
            self.best = {"matched": matched, "total": total, **spec}
        return complete and matched == file_count  # perfect — stop looking


async def album_search_fallback(session, catalog: CatalogClient,
                                discogs: DiscogsClient | None,
                                spotify: SpotifyClient | None, artist: Artist,
                                external: dict[str, str],
                                tag_artist: str, tag_album: str,
                                tag_titles: list[str], rows: list[MusicFile],
                                file_count: int, best_so_far: int) -> Release | None:
    """Exhaust EVERY source before a folder settles for less than green.
    Candidates come from: Deezer album search (several query shapes), Deezer
    track search (songs identify the album), Discogs and Spotify — by the
    artist's ID there when Wikidata knows it (never by name then), by name
    search otherwise. Each candidate's tracklist is evaluated against the
    folder's files; the winner must link MORE files than the current best,
    with a complete edition (every candidate track matched) preferred. Stops
    early on a perfect fit (complete edition linking every file)."""
    hunt = _Hunt(file_count, best_so_far)
    await _catalog_albums(hunt, catalog, tag_artist, tag_album, rows)
    if not hunt.perfect:
        await _catalog_tracks(hunt, catalog, tag_artist, tag_album, tag_titles, rows)
    if not hunt.perfect and discogs is not None:
        await _discogs(hunt, session, discogs, external, tag_artist, tag_album, rows)
    if not hunt.perfect and spotify is not None:
        await _spotify(hunt, spotify, external, tag_artist, tag_album, rows)

    # the mixes this hunt walked past, filed against the rows the artist
    # already has — the winner below may still be creating its row, and a
    # variant never brings a release into existence
    await artists.note_album_variants(session, artist.id, hunt.noticed)

    best_spec = hunt.best
    if best_spec is None:
        return None
    log.info("fallback winner via %s: %r (%d/%d) for %s — %s",
             best_spec["src"],
             best_spec.get("title") or best_spec.get("master", {}).get("title")
             or best_spec.get("hit", {}).get("title"),
             best_spec["matched"], best_spec["total"], tag_artist, tag_album)

    if best_spec["src"] in catalogs.SOURCES:
        return await _adopt_catalog_hit(session, catalog, artist, best_spec)
    if best_spec["src"] in ("discogs", "discogs_release"):
        return await _adopt_discogs(session, artist, best_spec, rows)
    return await _adopt_spotify(session, artist, best_spec)


async def _catalog_albums(hunt: _Hunt, catalog: CatalogClient, tag_artist: str,
                          tag_album: str, rows: list[MusicFile]) -> None:
    """Rung 1: catalog album search, most trusted catalogue first, several
    query shapes."""
    for query in _query_variants(tag_artist, tag_album):
        if hunt.perfect:
            break
        try:
            hits = await catalog.search_albums(query)
        except Exception as exc:
            log.error("album search failed for %r: %s", query, exc)
            continue
        for position, hit in enumerate(hits[:5]):
            title = hit.get("title", "")
            if album_score(title, tag_album) < ALBUM_MATCH_THRESHOLD:
                continue
            hit_artist = (hit.get("artist") or {}).get("name", "")
            artist_fit = fuzz.token_set_ratio(norm(tag_artist), norm(hit_artist))
            if artist_fit < 70 and not (position == 0
                                        and album_score(title, tag_album) >= 95):
                continue
            if hit["id"] in hunt.seen_album:
                continue
            hunt.seen_album.add(hit["id"])
            # the Atmos edition carries the same title and the same tracklist,
            # so it would score exactly like the stereo one and could win the
            # folder — a different mix is never the edition on disk
            sighting = variants.from_catalog(hit)
            if sighting is not None:
                hunt.noticed.append(sighting)
                continue
            try:
                tracks = await catalog.get_album_tracks(hit["id"], hit["source"])
            except Exception as exc:
                log.warning("%s tracklist of album %s could not be read: %s",
                            hit["source"], hit["id"], exc)
                continue
            matched = matching.eval_titles([t["title"] for t in tracks], rows)
            if hunt.consider(matched, len(tracks),
                             {"src": hit["source"], "album_id": hit["id"], "title": title}):
                hunt.perfect = True
                break


async def _catalog_tracks(hunt: _Hunt, catalog: CatalogClient, tag_artist: str,
                          tag_album: str, tag_titles: list[str],
                          rows: list[MusicFile]) -> None:
    """Rung 2: catalog track search — songs identify the album."""
    track_albums: dict[int, dict] = {}
    for track_title in tag_titles[:3]:
        try:
            hits = await catalog.search_tracks(f"{tag_artist} {track_title}")
        except Exception as exc:
            log.warning("track search failed for %r by %r: %s",
                        track_title, tag_artist, exc)
            continue
        for hit in hits[:8]:
            hit_artist = (hit.get("artist") or {}).get("name", "")
            if fuzz.token_set_ratio(norm(tag_artist), norm(hit_artist)) < 70:
                continue
            album = hit.get("album") or {}
            if album.get("id") and album["id"] not in hunt.seen_album:
                album["source"] = hit.get("source", "deezer")
                track_albums.setdefault(album["id"], album)
    for album_id, album in list(track_albums.items())[:4]:
        hunt.seen_album.add(album_id)
        try:
            tracks = await catalog.get_album_tracks(album_id, album["source"])
        except Exception as exc:
            log.warning("%s tracklist of album %s could not be read: %s",
                        album["source"], album_id, exc)
            continue
        matched = matching.eval_titles([t["title"] for t in tracks], rows)
        hit_title = album.get("title") or ""
        if hunt.consider(matched, len(tracks),
                         {"src": album["source"], "album_id": album_id,
                          "title": hit_title or tag_album},
                         name_ok=album_score(hit_title, tag_album)
                         >= ALBUM_MATCH_THRESHOLD):
            hunt.perfect = True
            break


async def _discogs(hunt: _Hunt, session, discogs: DiscogsClient, external: dict[str, str],
                   tag_artist: str, tag_album: str, rows: list[MusicFile]) -> None:
    """Rung 3: Discogs — by the artist's Discogs ID when known, name search
    otherwise."""
    masters = await _discogs_masters(session, discogs, external, tag_artist, tag_album)
    for master in masters:
        try:
            tracks = await discogs.master_tracklist(master["id"])
        except DiscogsError as exc:
            log.warning("discogs tracklist of master %s could not be read: %s",
                        master["id"], exc)
            continue
        matched = matching.eval_titles([t["title"] for t in tracks], rows)
        if hunt.consider(matched, len(tracks), {"src": "discogs", "master": master}):
            hunt.perfect = True
            break

    best_matched = hunt.best["matched"] if hunt.best else hunt.best_so_far
    if not hunt.perfect and masters and best_matched < hunt.file_count:
        await _discogs_versions(hunt, discogs, masters, rows)
    if not hunt.perfect:
        await _discogs_releases(hunt, discogs, tag_artist, tag_album, rows)


async def _discogs_masters(session, discogs: DiscogsClient, external: dict[str, str],
                           tag_artist: str, tag_album: str) -> list[dict]:
    try:
        if external.get("discogs"):
            masters = await discogs.cached_artist_masters(
                session, int(external["discogs"]))
            scored = sorted(
                ((album_score(m["title"], tag_album), m) for m in masters),
                key=lambda p: p[0], reverse=True,
            )
            return [m for s, m in scored[:3] if s >= 70]
        return [
            m for m in (await discogs.search_masters(tag_artist, tag_album))[:3]
            if album_score(m["title"], tag_album) >= ALBUM_MATCH_THRESHOLD
        ]
    except DiscogsError as exc:
        log.error("discogs fallback failed for %s — %s: %s",
                  tag_artist, tag_album, exc)
        return []


async def _discogs_versions(hunt: _Hunt, discogs: DiscogsClient, masters: list[dict],
                            rows: list[MusicFile]) -> None:
    """Rung 3b: the master tracklist is only the ORIGINAL release; the folder may
    hold a fuller edition (2CD reissue with covers/reprises) that exists solely
    as one of the master's versions."""
    for master in masters[:2]:
        if hunt.perfect:
            break
        try:
            versions = await discogs.master_versions(master["id"])
        except DiscogsError as exc:
            log.error("discogs versions failed for master %s: %s",
                      master["id"], exc)
            continue
        hunt.noticed += variants.from_discogs_versions(versions, master["title"])
        for version in version_candidates(versions):
            try:
                vtracks = await discogs.release_tracklist(version["id"])
            except DiscogsError as exc:
                log.warning("discogs tracklist of release %s could not be read: %s",
                            version["id"], exc)
                continue
            matched = matching.eval_titles([t["title"] for t in vtracks], rows)
            if hunt.consider(matched, len(vtracks),
                             {"src": "discogs_release", "master": master,
                              "tracks": vtracks}):
                hunt.perfect = True
                break


async def _discogs_releases(hunt: _Hunt, discogs: DiscogsClient, tag_artist: str,
                            tag_album: str, rows: list[MusicFile]) -> None:
    """Rung 3c: Discogs RELEASE search — a small/regional album with no master
    grouping (Kawasaki 3P's "25 Godina x Teatar &TD") never shows up in master
    search, but its release tracklist matches the folder."""
    try:
        found = [
            r for r in await discogs.search_releases(tag_artist, tag_album)
            if album_score(r["title"], tag_album) >= ALBUM_MATCH_THRESHOLD
        ]
    except DiscogsError as exc:
        log.error("discogs release search failed for %s — %s: %s",
                  tag_artist, tag_album, exc)
        found = []
    for rel in found[:3]:
        try:
            rtracks = await discogs.release_tracklist(rel["id"])
        except DiscogsError as exc:
            log.warning("discogs tracklist of release %s could not be read: %s",
                        rel["id"], exc)
            continue
        matched = matching.eval_titles([t["title"] for t in rtracks], rows)
        if hunt.consider(matched, len(rtracks),
                         {"src": "discogs_release", "master": rel,
                          "tracks": rtracks}):
            hunt.perfect = True
            break


async def _spotify(hunt: _Hunt, spotify: SpotifyClient, external: dict[str, str],
                   tag_artist: str, tag_album: str, rows: list[MusicFile]) -> None:
    """Rung 4: Spotify — by the artist's Spotify ID when known."""
    hits: list[dict] = []
    try:
        if external.get("spotify"):
            hits = await spotify.artist_albums(external["spotify"])
            scored = sorted(
                ((album_score(a["title"], tag_album), a) for a in hits),
                key=lambda p: p[0], reverse=True,
            )
            hits = [a for s, a in scored[:3] if s >= 70]
        else:
            hits = [
                a for a in (await spotify.search_albums(tag_artist, tag_album))[:3]
                if album_score(a["title"], tag_album) >= ALBUM_MATCH_THRESHOLD
            ]
    except SpotifyCooldown as exc:
        log.debug("spotify fallback skipped for %s — %s: %s", tag_artist, tag_album, exc)
    except SpotifyError as exc:
        log.error("spotify fallback failed for %s — %s: %s",
                  tag_artist, tag_album, exc)
    for hit in hits:
        try:
            tracks = await spotify.album_tracks(hit["id"])
        except SpotifyCooldown as exc:
            log.debug("spotify fallback skipped for %s — %s: %s", tag_artist, tag_album, exc)
            break
        except SpotifyError as exc:
            log.warning("spotify tracklist of album %s could not be read: %s",
                        hit["id"], exc)
            continue
        matched = matching.eval_titles([t["title"] for t in tracks], rows)
        if hunt.consider(matched, len(tracks), {"src": "spotify", "hit": hit}):
            break


async def _adopt_catalog_hit(session, catalog: CatalogClient, artist: Artist,
                             best_spec: dict) -> Release | None:
    source = best_spec["src"]
    try:
        details = await catalog.get_album(best_spec["album_id"], source)
    except Exception as exc:
        log.warning("%s album %s could not be read, adopted from the search hit: %s",
                    source, best_spec["album_id"], exc)
        details = {}
    return await _adopt_album_hit(
        session, artist, best_spec["album_id"],
        details.get("title") or best_spec["title"],
        details.get("record_type"), details.get("cover_medium"),
        details.get("release_date"), source=source,
    )


async def _adopt_discogs(session, artist: Artist, best_spec: dict,
                         rows: list[MusicFile]) -> Release | None:
    master = best_spec["master"]
    existing = (await session.execute(
        select(Release).where(Release.discogs_id == master["id"])
    )).scalars().first()
    if existing is not None:
        if existing.artist_id != artist.id:
            return None
        if best_spec["src"] == "discogs_release":
            current = (await session.execute(
                select(Track.id).where(Track.release_id == existing.id)
            )).scalars().all()
            if len(current) != best_spec["total"]:
                linked = (await session.execute(
                    select(MusicFile.id)
                    .join(Track, MusicFile.track_id == Track.id)
                    .where(Track.release_id == existing.id)
                )).scalars().all()
                if not linked or set(linked) <= {row.id for row in rows}:
                    # same master, fuller edition of THIS folder's album:
                    # swap the tracklist in place instead of forking a row
                    if linked:
                        await session.execute(
                            update(MusicFile)
                            .where(MusicFile.id.in_(linked))
                            .values(track_id=None)
                        )
                    await session.execute(
                        delete(Track).where(Track.release_id == existing.id)
                    )
                    _materialize_edition(session, existing, best_spec)
                    await session.flush()
                    return existing
                # its files belong to another folder — a same-titled
                # DIFFERENT album owns this master row: free it
                existing.discogs_id = None
                await _drop_if_husk(session, existing)
                await session.flush()
            else:
                return existing
        elif (existing.track_count
                and existing.track_count != best_spec["total"]):
            # a mis-merge glued this master onto a same-titled DIFFERENT
            # album (Australian vs international High Voltage): free the
            # master and give the real album its own row
            existing.discogs_id = None
            await _drop_if_husk(session, existing)
            await session.flush()
        else:
            return existing
    release = Release(
        artist_id=artist.id,
        title=master["title"],
        discogs_id=master["id"],
        release_date=str(master["year"]) if master.get("year") else None,
        record_type="single" if " / " in master["title"] else None,
        cover_url=master.get("thumb") or None,
    )
    session.add(release)
    await session.flush()
    if best_spec["src"] == "discogs_release":
        _materialize_edition(session, release, best_spec)
        await session.flush()
    return release


async def _adopt_spotify(session, artist: Artist, best_spec: dict) -> Release | None:
    hit = best_spec["hit"]
    existing = (await session.execute(
        select(Release).where(Release.spotify_id == hit["id"])
    )).scalars().first()
    if existing is not None:
        return existing if existing.artist_id == artist.id else None
    release = Release(
        artist_id=artist.id,
        title=hit["title"],
        spotify_id=hit["id"],
        release_date=hit.get("release_date"),
        record_type=hit.get("record_type"),
        cover_url=hit.get("cover_url"),
    )
    session.add(release)
    await session.flush()
    return release
