"""Wikidata/Wikipedia ruling over the streaming catalogs: the album QID is the
release's durable identity, the label the canonical title, P577 the curated
ORIGINAL release date and the article's listing the canonical track spelling.
One SPARQL query per artist; every article behind it is fetched exactly once,
ever, through a persistent per-QID cache. The Deezer tracklist refresh lives
here because it is the rung directly below — it runs first and Wikipedia
spelling then outranks it."""

import logging

from sqlalchemy import func, select

from opus.music.classify import classify_release, pressing_of
from opus.music.metadata import wikidata as wd
from opus.music.metadata.dedupe import TITLE_MATCH_THRESHOLD, _year
from opus.music.metadata.deezer import DeezerClient, DeezerError, DeezerNotFound
from opus.music.metadata.tracklists import strip_edition_qualifier
from opus.models import MusicFile, Release, Track, WikiAlbumCache
from opus.music.textnorm import album_score, same_name, title_score

log = logging.getLogger("opus.discography")

WIKI_TITLE_THRESHOLD = 65

_wikipedia_info: dict[str, dict] = {}

# a Deezer album that has no tracklist says so on every pass; once is enough
_no_deezer_tracklist: set[int] = set()


async def get_wiki_info(session, client: wd.WikidataClient, qid: str) -> dict:
    """Per-QID Wikipedia album data through a persistent Postgres cache —
    each article is fetched exactly once, ever. A Wikipedia hiccup degrades
    to empty info for this call and retries on a later run."""
    if qid in _wikipedia_info:
        return _wikipedia_info[qid]
    row = await session.get(WikiAlbumCache, qid)
    if row is not None and row.external_ids is not None:
        info = {"tracklist": row.tracklist, "record_type": row.record_type,
                "external_ids": row.external_ids}
    else:
        try:
            info = await client.wikipedia_album_info(qid)
        except Exception as exc:
            log.error("wikipedia album info failed for %s: %s", qid, exc)
            return {"tracklist": None, "record_type": None, "external_ids": {}}
        if row is not None:
            row.record_type = info["record_type"]
            row.tracklist = info["tracklist"]
            row.external_ids = info["external_ids"]
        else:
            session.add(WikiAlbumCache(qid=qid, record_type=info["record_type"],
                                       tracklist=info["tracklist"],
                                       external_ids=info["external_ids"]))
        await session.flush()
    _wikipedia_info[qid] = info
    return info


async def _refresh_deezer_tracks(session, releases: list[Release]) -> int:
    """Tracks fetched before the release had its Deezer id keep the weaker
    source's titles forever — Discogs is user-contributed Title Case ('Kada
    Kreneš Na Put') while Deezer is the naming canon. A deezer-backed release
    whose tracks carry no Deezer track ids adopts the Deezer tracklist once,
    positionally; manually edited titles stay. Wikipedia spelling still
    outranks — it runs after this."""
    refreshed = 0
    client = None
    try:
        for release in releases:
            if release.deezer_id is None:
                continue
            tracks = list((await session.execute(
                select(Track).where(Track.release_id == release.id)
                .order_by(Track.position)
            )).scalars())
            if not tracks or any(t.deezer_id is not None for t in tracks):
                continue
            positions = [t.position for t in tracks]
            # per-disc numbering repeats positions — positional adoption is
            # only safe when the order is unambiguous
            if positions != sorted(set(positions)):
                continue
            if release.deezer_id in _no_deezer_tracklist:
                continue
            if client is None:
                client = DeezerClient()
            try:
                rows = await client.get_album_tracks(release.deezer_id)
            except DeezerNotFound as exc:
                _no_deezer_tracklist.add(release.deezer_id)
                log.warning("deezer has no tracklist for release %s: %s", release.id, exc)
                continue
            except DeezerError as exc:
                log.error("deezer tracklist refresh failed for release %s: %s",
                          release.id, exc)
                continue
            if len(rows) != len(tracks):
                continue
            titles = strip_edition_qualifier([r["title"] for r in rows])
            for track, row, title in zip(tracks, rows, titles):
                track.deezer_id = row["id"]
                if row.get("duration"):
                    track.duration_sec = row["duration"]
                if not track.title_manual and track.title != title:
                    track.title = title
                    refreshed += 1
    finally:
        if client is not None:
            await client.close()
    return refreshed


async def _wikidata_album_info(session, artist_qid: str | None,
                               releases: list[Release]) -> tuple[int, int]:
    """Wikidata is the naming/date REFERENCE. One SPARQL query fetches every
    album the artist performed; our releases match against those locally. On a
    match the Wikidata label becomes the canonical title ("MTV Unplugged", not
    "MTV Unplugged (Live)") and P577 the curated ORIGINAL release date —
    streaming sources carry edition noise and reissue dates. The album QID is
    the release's durable identity."""
    if artist_qid is None:
        return 0, 0
    pending = [
        r for r in releases
        if r.wikidata_id is None
        and classify_release(r.title, r.record_type) not in ("single", "ep")
    ]
    resolved_map = {r.wikidata_id: r for r in releases if r.wikidata_id}
    if not pending and not resolved_map:
        return 0, 0

    client = wd.WikidataClient()
    try:
        albums = [a for a in await client.albums_by_performer(artist_qid) if a["label"]]
        if not albums:
            return 0, 0
        adopted_ids: set[tuple[str, object]] = set()
        # refresh already-resolved releases from the authority (repairs dates a
        # lower-ranked source may have overwritten in the meantime)
        for album in albums:
            release = resolved_map.get(album["qid"])
            if release is not None:
                await _apply_authority(session, client, release, album, adopted_ids)
        file_counts = await _file_counts(session, releases)
        resolved = await _resolve_pending(session, client, releases, pending, albums,
                                          dict(resolved_map), file_counts, adopted_ids)
        retitled = await _apply_wikipedia_titles(session, client, releases,
                                                 file_counts)
    finally:
        await client.close()
    return resolved, retitled


async def _file_counts(session, releases: list[Release]) -> dict[int, int]:
    if not releases:
        return {}
    return dict((await session.execute(
        select(Track.release_id, func.count(MusicFile.id))
        .join(MusicFile, MusicFile.track_id == Track.id)
        .where(Track.release_id.in_([r.id for r in releases]))
        .group_by(Track.release_id)
    )).all())


async def _resolve_pending(session, client: wd.WikidataClient, releases: list[Release],
                           pending: list[Release], albums: list[dict],
                           holders: dict[str, Release], file_counts: dict[int, int],
                           adopted_ids: set[tuple[str, object]]) -> int:
    resolved = 0
    # the row that HOLDS the files owns the identity: it resolves first,
    # and it may take a QID from a file-less duplicate holder
    for release in sorted(pending, key=lambda r: file_counts.get(r.id, 0), reverse=True):
        best = _best_album(release, albums, holders, file_counts)
        if best is None:
            continue
        if await _held_by_another_artist(session, best["qid"], release, releases):
            continue
        await _take_identity(session, release, best["qid"], holders)
        resolved += 1
        await _apply_authority(session, client, release, best, adopted_ids)
    return resolved


def _may_take(holder: Release | None, release: Release, file_counts: dict[int, int]) -> bool:
    return holder is None or (file_counts.get(holder.id, 0) == 0
                              and file_counts.get(release.id, 0) > 0)


def _best_album(release: Release, albums: list[dict], holders: dict[str, Release],
                file_counts: dict[int, int]) -> dict | None:
    best, best_score = None, 0.0
    for album in albums:
        if not _may_take(holders.get(album["qid"]), release, file_counts):
            continue
        score = _corroborated_score(release, album)
        if score is not None and score > best_score:
            best, best_score = album, score
    if best is None or best_score < TITLE_MATCH_THRESHOLD:
        return None
    return best


def _corroborated_score(release: Release, album: dict) -> float | None:
    # A pressing answers to the name of the record it is a pressing
    # of. `Killers (2015 Remaster)` is dated 2018 by the streaming
    # source and 1981 by the authority, and the corroboration below
    # would throw the pair out over exactly the difference it is
    # here to correct — the reissue could never reach the date that
    # would fix it.
    label = album["label"] or ""
    named = same_name(release.title, label)
    pressing = pressing_of(release.title)
    if not named and pressing:
        # `same_name` is equality after tidying, and a pressing's
        # base name misses it on a space: `Pinups` is not `Pin Ups`,
        # though they score 92 of 100. For a title that has already
        # said it is a pressing, that score IS the agreement — the
        # one thing that could corroborate it otherwise is the year,
        # and the year is what is wrong.
        named = (same_name(pressing, label)
                 or album_score(pressing, label) >= TITLE_MATCH_THRESHOLD)
    # a near-title match is only evidence WITH year corroboration:
    # years apart is a different record ("The Album" vs the 2008
    # "The Albums" box), and an unknown year is no corroboration
    # at all — the undated 'Vojko' item must not be bought by
    # 'Dvojko' scoring 90.9
    if not named:
        release_year, album_year = _year(release.release_date), _year(album.get("date"))
        if (release_year is None or album_year is None
                or abs(release_year - album_year) > 1):
            return None
    return max(album_score(release.title, label),
               album_score(pressing, label) if pressing else 0.0)


async def _held_by_another_artist(session, qid: str, release: Release,
                                  releases: list[Release]) -> bool:
    # a shared album (Riding with the King: B.B. King & Clapton) may
    # already carry the QID on the other principal's row — one row
    # owns the identity, this one keeps its catalog data
    with session.no_autoflush:
        foreign = (await session.execute(
            select(Release.id)
            .where(Release.wikidata_id == qid,
                   Release.id.notin_([r.id for r in releases if r.id]))
        )).first()
    if foreign is None:
        return False
    log.info("QID %s already held by release %s — release %s "
             "keeps its own catalog data", qid, foreign[0], release.id)
    return True


async def _take_identity(session, release: Release, qid: str,
                         holders: dict[str, Release]) -> None:
    previous = holders.get(qid)
    if previous is not None:
        # the freed holder must reach the DB before the QID moves,
        # or the unique constraint fires inside the same flush
        previous.wikidata_id = None
        await session.flush()
    release.wikidata_id = qid
    holders[qid] = release


async def _apply_authority(session, client: wd.WikidataClient, release: Release,
                           album: dict, adopted_ids: set[tuple[str, object]]) -> None:
    _adopt_label(release, album)
    _apply_wikidata_date(release, album["date"])
    # P31 live/compilation typing is trustworthy; the sloppy direction
    # is box sets and video releases typed plain 'album' — only those
    # are worth a Wikipedia infobox lookup, which outranks P31
    p31 = album.get("record_type")
    if p31 in ("live", "compilation"):
        release.record_type = p31
        return
    info = await get_wiki_info(session, client, album["qid"])
    if info["record_type"]:
        release.record_type = info["record_type"]
    elif p31:
        release.record_type = p31
    await _adopt_external_ids(session, release, info.get("external_ids") or {}, adopted_ids)


def _adopt_label(release: Release, album: dict) -> None:
    # some Wikidata labels are essay-length garbage — never adopt one
    # the title column cannot hold
    if len(album["label"]) <= 500:
        release.title = album["label"]
    else:
        log.warning("Wikidata label for %s is %d chars — release %s "
                    "keeps %r", album["qid"], len(album["label"]),
                    release.id, release.title)


async def _adopt_external_ids(session, release: Release, ids: dict,
                              adopted_ids: set[tuple[str, object]]) -> None:
    # the canonical album's ids on the other services come straight
    # from the entity claims — targeted linkage, no searching
    for source, column, cast in (("deezer", "deezer_id", int),
                                 ("spotify", "spotify_id", str),
                                 ("discogs", "discogs_id", int)):
        if getattr(release, column) is not None or not ids.get(source):
            continue
        try:
            value = cast(ids[source])
        except ValueError:
            continue
        if (column, value) in adopted_ids:
            continue
        with session.no_autoflush:
            holder = (await session.execute(
                select(Release.id)
                .where(getattr(Release, column) == value)
            )).first()
        if holder is None:
            setattr(release, column, value)
            adopted_ids.add((column, value))


async def _apply_wikipedia_titles(session, client: wd.WikidataClient,
                                  releases: list[Release],
                                  file_counts: dict[int, int]) -> int:
    """Wikipedia outranks Deezer/Discogs/Spotify for track SPELLING — Discogs
    is user-contributed and carries typos. Applied only when the article's
    listing matches the release track-for-track; a low per-track score means a
    misaligned or wrong-article list, so that title stays. Manually edited
    titles are never touched."""
    retitled = 0
    for release in releases:
        qid = release.wikidata_id
        # spelling corrections only matter where files exist — fetching the
        # article for every unowned catalog row made enrich crawl
        if qid is None or not file_counts.get(release.id):
            continue
        tracks = list((await session.execute(
            select(Track).where(Track.release_id == release.id).order_by(Track.position)
        )).scalars())
        if not tracks:
            continue
        titles = (await get_wiki_info(session, client, qid))["tracklist"]
        if titles is None:
            continue
        if len(titles) != len(tracks):
            log.debug("wikipedia tracklist for %s has %d titles, release %s has %d tracks",
                      qid, len(titles), release.id, len(tracks))
            continue
        for track, title in zip(tracks, titles):
            if track.title_manual or track.title == title:
                continue
            if title_score(track.title, title) < WIKI_TITLE_THRESHOLD or len(title) > 500:
                log.info("wikipedia title skipped on release %s position %s: %r vs %r",
                         release.id, track.position, track.title, title)
                continue
            track.title = title
            retitled += 1
    return retitled


def _apply_wikidata_date(release: Release, wikidata_date: str | None):
    """P577 wins — except when it only knows the year and the current value is
    a full date of that same year (keep the better precision)."""
    if not wikidata_date:
        return
    current = release.release_date
    if (len(wikidata_date) == 4 and current
            and current[:4] == wikidata_date and len(current) > 4):
        return
    release.release_date = wikidata_date
