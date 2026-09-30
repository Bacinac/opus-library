"""Multi-source discography sync: merge Deezer (canonical), Discogs (physical/
Jugoton era) and Spotify into the artist's releases table — the single list
that defines every album, which the library is compared against. Secondary
sources participate only when configured; a release found only on a secondary
source carries that source's ID and gets its tracklist from it. This module is
the story of one sync: gather the candidates, fold them into the existing rows,
prune what the sources no longer credit to this artist, then let the dedupe and
authority passes rule on what is left.

Discogs specifics: artist listings carry no format for masters, so untyped
masters are typed once from master details (track count: <=4 single, 5-6 EP,
else album). The master year is the ORIGINAL release year — streaming dates
are often reissues."""

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from opus.db import SessionLocal
from opus.music.metadata.authority import _refresh_deezer_tracks, _wikidata_album_info
from opus.music.metadata.dedupe import (
    _apply_secondary_info,
    _fill_source_id,
    _find_by_source_id,
    _match_by_title,
    _merge_legacy_duplicates,
)
from opus.music.metadata.deezer import DeezerClient, DeezerError, DeezerNotFound
from opus.music.metadata.discogs import DiscogsClient, DiscogsError
from opus.music.metadata.spotify import SpotifyClient, SpotifyCooldown, SpotifyError
from opus.music.metadata.tracklists import DiscographyError
from opus.models import (
    Artist,
    ArtistExternalId,
    MusicDownload,
    MusicFile,
    Release,
    ReleaseStatus,
    Track,
)
from opus.settings_store import RuntimeConfig, current_runtime
from opus import passes

log = logging.getLogger("opus.discography")

_kept_strays: set[int] = set()


def spawn_sync(artist_id: int):
    passes.spawn(_sync_logged(artist_id))


async def _sync_logged(artist_id: int):
    try:
        await sync_artist(artist_id)
    except Exception:
        log.exception("discography sync failed for artist %s", artist_id)


# How many years of an artist's working life the streaming catalogue may be
# missing before it counts as not knowing them.
ERA_GAP_YEARS = 10
# A person's begin year is when they were born; a group's is when it formed.
# Nobody's first record is at eight.
FIRST_RECORD_AT = 20


def _streaming_is_blind(candidates: list[dict], career_from: int | None) -> bool:
    """Whether the streaming catalogues have missed this artist's early work.

    Counting rows was the old test and it is the wrong question. Deezer knows
    Rundek's last four records and nothing of the twenty years before them, and
    seven rows was enough to look like a catalogue — so the Discogs rung, which
    exists for exactly the Jugoton era, never fired for the artists it was built
    for. What gives it away is not how few records there are but WHEN they
    start: a catalogue that begins two decades into a working life is a
    catalogue with the beginning cut off."""
    if len(candidates) < 5:
        return True
    if career_from is None:
        return False
    years = [int(c["release_date"][:4]) for c in candidates
             if (c.get("release_date") or "")[:4].isdigit()]
    return bool(years) and min(years) - career_from >= ERA_GAP_YEARS


async def _gather_candidates(
    name: str, deezer_id: int | None, external: dict,
    config, typed_discogs_ids: set[int], career_from: int | None = None,
) -> tuple[list[dict], int | None, bool, set[int]]:
    candidates: list[dict] = []
    stray_deezer_ids: set[int] = set()
    if deezer_id is not None:
        candidates, stray_deezer_ids = await _deezer_candidates(deezer_id)

    # the Discogs BULK rung exists for artists the streaming catalogs barely
    # know (Jugoton-era) — a rich Deezer catalog makes the thousands of
    # Discogs pressings pure bloat; canonical albums link their Discogs
    # master via Wikidata claims instead
    discovered_discogs_id: int | None = None
    discogs_synced = bool(config.get("discogs_token")) and _streaming_is_blind(
        candidates, career_from)
    if discogs_synced:
        found, discovered_discogs_id = await _discogs_candidates(
            name, external.get("discogs"), config.get("discogs_token"), typed_discogs_ids)
        candidates += found

    if (external.get("spotify") and config.get("spotify_client_id")
            and config.get("spotify_client_secret")):
        candidates += await _spotify_candidates(external["spotify"], config)

    return candidates, discovered_discogs_id, discogs_synced, stray_deezer_ids


def _candidate(source: str, external_id, title: str, release_date: str | None,
               record_type: str | None, cover_url: str | None) -> dict:
    return {"source": source, "external_id": external_id, "title": title,
            "release_date": release_date, "record_type": record_type,
            "cover_url": cover_url}


async def _deezer_candidates(deezer_id: int) -> tuple[list[dict], set[int]]:
    candidates: list[dict] = []
    strays: set[int] = set()
    client = DeezerClient()
    try:
        for album in await client.get_artist_albums(deezer_id):
            # a Deezer artist page mixes in other acts' work (the 'Buco'
            # page carries a NAREK NK rap album and two Turkish singles);
            # the album payload names the PRINCIPAL artist — only albums
            # this artist owns become candidates, the rest are strays
            try:
                detail = await client.get_album(album["id"])
            except DeezerNotFound:
                continue
            except DeezerError as exc:
                log.warning("deezer album %s on page %s could not be read: %s",
                            album["id"], deezer_id, exc)
                continue
            principal = (detail.get("artist") or {}).get("id")
            if principal is not None and principal != deezer_id:
                log.info("stray on deezer page %s: %r belongs to artist %s",
                         deezer_id, album["title"], principal)
                strays.add(album["id"])
                continue
            candidates.append(_candidate(
                "deezer", album["id"], album["title"], album.get("release_date"),
                album.get("record_type"), album.get("cover_medium")))
    finally:
        await client.close()
    return candidates, strays


async def _discogs_candidates(name: str, discogs_id: str | int | None, token: str,
                              typed_discogs_ids: set[int]) -> tuple[list[dict], int | None]:
    discogs = DiscogsClient(token)
    try:
        discovered: int | None = None
        if discogs_id is None:
            discovered = discogs_id = await discogs.find_artist_id(name)
        if discogs_id is None:
            return [], None
        masters = await _discogs_masters(discogs, name, int(discogs_id))
        candidates: list[dict] = []
        typed_any = False
        for master in masters:
            candidate, typed = await _master_candidate(discogs, master, typed_discogs_ids)
            candidates.append(candidate)
            typed_any = typed_any or typed
        if typed_any:
            async with SessionLocal() as cache_session:
                await discogs.store_masters(cache_session, int(discogs_id), masters)
                await cache_session.commit()
        return candidates, discovered
    finally:
        await discogs.close()


async def _discogs_masters(discogs: DiscogsClient, name: str, discogs_id: int) -> list[dict]:
    # the 'Various' pseudo-artist (194) 404s its releases endpoint — a
    # broken Discogs rung degrades to no Discogs candidates, it must not
    # fail the whole sync
    try:
        async with SessionLocal() as cache_session:
            masters = await discogs.cached_artist_masters(cache_session, discogs_id)
            await cache_session.commit()
    except DiscogsError as exc:
        log.error("discogs masters failed for %r (%s): %s", name, discogs_id, exc)
        return []
    return masters


async def _master_candidate(discogs: DiscogsClient, master: dict,
                            typed_discogs_ids: set[int]) -> tuple[dict, bool]:
    title = master["title"]
    year = master.get("year")
    # ex-Yu 7" singles are titled "A side / B side"; typed results persist
    # inside the masters cache so the per-master details sweep runs once,
    # not every sync
    record_type = master.get("record_type") or ("single" if " / " in title else None)
    typed = False
    if record_type is None and master["id"] not in typed_discogs_ids:
        details = await _master_details(discogs, master)
        if details:
            year = details.get("year") or year
            record_type = _type_by_track_count(details["track_count"])
            master["record_type"] = record_type
            master["year"] = year
            typed = True
    return _candidate("discogs", master["id"], title, str(year) if year else None,
                      record_type, master.get("thumb") or None), typed


async def _master_details(discogs: DiscogsClient, master: dict) -> dict:
    try:
        return await discogs.master_details(master["id"])
    except DiscogsError as exc:
        # Discogs 500s on some masters — don't fail the artist
        log.error("master details failed for %s (%s): %s",
                  master["title"], master["id"], exc)
        return {}


def _type_by_track_count(count: int | None) -> str:
    if count and count <= 4:
        return "single"
    if count in (5, 6):
        return "ep"
    return "album"


async def _spotify_candidates(spotify_id: str, config) -> list[dict]:
    spotify = SpotifyClient(config.get("spotify_client_id"),
                            config.get("spotify_client_secret"))
    try:
        return [_candidate("spotify", album["id"], album["title"],
                           album.get("release_date"), album.get("record_type"),
                           album.get("cover_url"))
                for album in await spotify.artist_albums(spotify_id)]
    except SpotifyCooldown as exc:
        log.debug("spotify discography skipped: %s", exc)
    except SpotifyError as exc:
        log.error("spotify discography failed: %s", exc)
    finally:
        await spotify.close()
    return []


@dataclass(frozen=True)
class _Subject:
    name: str
    deezer_id: int | None
    wikidata_id: str | None
    career_from: int | None
    external: dict[str, str]
    typed_discogs_ids: set[int]
    config: RuntimeConfig


def _career_from(artist: Artist) -> int | None:
    if artist.begin_year is None:
        return None
    return artist.begin_year + (FIRST_RECORD_AT if artist.artist_type == "person" else 0)


async def _load_subject(artist_id: int) -> _Subject:
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        if artist is None:
            raise DiscographyError(f"artist {artist_id} not found")
        config = await current_runtime()
        external = {
            e.source: e.external_id
            for e in (await session.execute(
                select(ArtistExternalId).where(ArtistExternalId.artist_id == artist_id)
            )).scalars()
        }
        typed_discogs_ids = {
            r.discogs_id
            for r in (await session.execute(
                select(Release).where(Release.artist_id == artist_id)
            )).scalars()
            if r.discogs_id is not None and r.record_type is not None
        }
        return _Subject(name=artist.name, deezer_id=artist.deezer_id,
                        wikidata_id=artist.wikidata_id, career_from=_career_from(artist),
                        external=external, typed_discogs_ids=typed_discogs_ids,
                        config=config)


async def sync_artist(artist_id: int) -> dict:
    subject = await _load_subject(artist_id)
    candidates, discovered_discogs_id, discogs_synced, stray_deezer_ids = (
        await _gather_candidates(subject.name, subject.deezer_id, subject.external,
                                 subject.config, subject.typed_discogs_ids,
                                 subject.career_from)
    )

    async with SessionLocal() as session:
        if discovered_discogs_id is not None:
            await _remember_discogs_id(session, artist_id, discovered_discogs_id)
        releases = list((await session.execute(
            select(Release).where(Release.artist_id == artist_id)
        )).scalars())
        foreign = await _foreign_deezer_ids(session, artist_id)
        added, linked = _fold_candidates(session, artist_id, releases, candidates, foreign)
        if subject.deezer_id is not None:
            stray_deezer_ids |= await _unlisted_strays(releases, candidates, subject.deezer_id)
        await _prune_strays(session, artist_id, releases, stray_deezer_ids)
        merged = await _merge_legacy_duplicates(session, releases)
        retracked = await _refresh_deezer_tracks(session, releases)
        dated, retitled = await _wikidata_album_info(session, subject.wikidata_id, releases)
        if not discogs_synced:
            await _purge_discogs_bloat(session, artist_id, releases)
        await session.commit()

    log.info("artist %s discography synced: %d candidates, %d added, %d linked, "
             "%d merged, %d deezer-retracked, %d wikidata-dated, "
             "%d wikipedia-retitled",
             artist_id, len(candidates), added, linked, merged, retracked,
             dated, retitled)
    return {"added": added, "linked": linked, "merged": merged,
            "retracked": retracked, "dated": dated, "retitled": retitled}


async def _remember_discogs_id(session, artist_id: int, discogs_id: int) -> None:
    await session.execute(
        insert(ArtistExternalId)
        .values(artist_id=artist_id, source="discogs", external_id=str(discogs_id))
        .on_conflict_do_nothing())


async def _foreign_deezer_ids(session, artist_id: int) -> set[int]:
    # deezer_id is globally unique and Deezer lists shared compilations
    # under every contributing artist — never create a duplicate row
    return {
        row[0] for row in (await session.execute(
            select(Release.deezer_id).where(
                Release.deezer_id.is_not(None), Release.artist_id != artist_id
            )
        )).all()
    }


def _fold_candidates(session, artist_id: int, releases: list[Release],
                     candidates: list[dict], foreign_deezer_ids: set[int]) -> tuple[int, int]:
    added = linked = 0
    for cand in candidates:
        if cand["source"] == "deezer" and cand["external_id"] in foreign_deezer_ids:
            continue
        existing = _find_by_source_id(releases, cand)
        if existing is not None:
            _apply_secondary_info(existing, cand)
            continue
        existing = _match_by_title(releases, cand["title"])
        if existing is not None:
            if _fill_source_id(existing, cand):
                linked += 1
            _apply_secondary_info(existing, cand)
            continue
        release = Release(
            artist_id=artist_id,
            title=cand["title"],
            release_date=cand.get("release_date"),
            record_type=cand.get("record_type"),
            cover_url=cand.get("cover_url"),
        )
        _fill_source_id(release, cand)
        session.add(release)
        releases.append(release)
        added += 1
    return added, linked


async def _unlisted_strays(releases: list[Release], candidates: list[dict],
                           deezer_id: int) -> set[int]:
    # rows the page does NOT list are suspects too: populated from a
    # wrong/ghost page before the identity was corrected (Genesis via a
    # 511-fan bootleg aggregator), or since removed by Deezer — ask the
    # album itself who its principal artist is
    listed = {c["external_id"] for c in candidates if c["source"] == "deezer"}
    strays: set[int] = set()
    client = DeezerClient()
    try:
        for release in releases:
            if release.deezer_id is None or release.deezer_id in listed:
                continue
            try:
                detail = await client.get_album(release.deezer_id)
            except DeezerNotFound:
                continue
            except DeezerError as exc:
                log.warning("deezer album %s of release %s could not be read: %s",
                            release.deezer_id, release.id, exc)
                continue
            principal = (detail.get("artist") or {}).get("id")
            if principal is not None and principal != deezer_id:
                strays.add(release.deezer_id)
    finally:
        await client.close()
    return strays


async def _file_count(session, release_id: int) -> int:
    return (await session.execute(
        select(func.count()).select_from(MusicFile)
        .join(Track, Track.id == MusicFile.track_id)
        .where(Track.release_id == release_id)
    )).scalar()


async def _holds_anything(session, release: Release) -> bool:
    if release.status != ReleaseStatus.NONE or await _file_count(session, release.id):
        return True
    return bool((await session.execute(
        select(func.count()).select_from(MusicDownload)
        .where(MusicDownload.release_id == release.id)
    )).scalar())


async def _prune_strays(session, artist_id: int, releases: list[Release],
                        stray_deezer_ids: set[int]) -> None:
    # strays that leaked in before the principal check existed: rows the
    # Deezer page still lists but credits to someone else — deleted only
    # while nothing local holds on to them (no files, no downloads)
    pruned = 0
    for release in list(releases):
        if release.deezer_id not in stray_deezer_ids:
            continue
        if await _holds_anything(session, release):
            if release.id not in _kept_strays:
                _kept_strays.add(release.id)
                log.warning("stray %r (deezer %s) holds files/downloads — "
                            "kept, review manually",
                            release.title, release.deezer_id)
            continue
        await session.delete(release)
        releases.remove(release)
        pruned += 1
    if pruned:
        log.info("pruned %d stray deezer rows for artist %s", pruned, artist_id)


async def _purge_discogs_bloat(session, artist_id: int, releases: list[Release]) -> None:
    # the bulk rung was skipped as bloat — rows only Discogs ever
    # claimed, that nothing owns and no identity corroborates, go
    purged = 0
    for release in list(releases):
        if (release.discogs_id is None or release.deezer_id is not None
                or release.spotify_id is not None
                or release.wikidata_id is not None
                or release.status != ReleaseStatus.NONE):
            continue
        if await _file_count(session, release.id):
            continue
        await session.delete(release)
        releases.remove(release)
        purged += 1
    if purged:
        log.info("purged %d discogs-only bloat rows for artist %s",
                 purged, artist_id)
