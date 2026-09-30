"""Artist identity: a folder's tag name resolves to exactly one `artists` row.
The DB is asked first, then the catalog, then Wikidata, then
Discogs as the last resort — and a candidate is trusted only once the folder's
album, and for a homonym its actual songs, turn up in that candidate's own
catalog. A resolved artist commits immediately with a baseline discography:
everything downstream (folder match, post-scan enrich) needs the row to exist
before the folder's own outcome is known."""

import logging

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from opus.music.library import matching
from opus.music.metadata import deezer as deezer_module
from opus.music.metadata import variants
from opus.music.metadata import wikidata as wd
from opus.music.metadata import catalog as catalogs
from opus.music.metadata.catalog import CatalogClient
from opus.music.metadata.deezer import DeezerClient, DeezerError, DeezerNotFound
from opus.music.metadata.discogs import DiscogsClient, DiscogsError
from opus.models import Artist, ArtistExternalId, Release
from opus.music.textnorm import (album_score, artist_key, clean_display_name,
                            same_artist)

log = logging.getLogger("opus.libimport")

# 90 was unreachable for real-world variants ("Yusuf / Cat Stevens" = 78.6,
# "GORAN BARE & PLAĆENICI" vs "Bare i plaćenici" = 83.3); sub-90 candidates
# are safe because they must validate against the folder's album title
ARTIST_MATCH_THRESHOLD = 78
ARTIST_TRUSTED_SCORE = 90
ALBUM_MATCH_THRESHOLD = 85

# artists this scan created — they carry no metadata yet, so the finale
# enriches exactly them even on the no-chain follow-up scan
created_artist_ids: set[int] = set()


def _artist_name_score(tag_name: str, candidate: str) -> float:
    # token_sort_ratio, not token_set_ratio: the set variant scores 100 for
    # supersets like "Made famous by Amy Winehouse" (tribute/karaoke pages)
    return fuzz.token_sort_ratio(artist_key(tag_name), artist_key(candidate))


async def _owned_albums(catalog: CatalogClient, albums: list[dict],
                        owner_id: int) -> list[dict]:
    """A catalog artist page lists albums whose PRINCIPAL artist is someone
    else (Deezer's 'Buco' page carries a NAREK NK rap album and two Turkish
    singles). Keep only what the artist owns: a catalogue above Deezer carries
    the principal in the page payload, Deezer needs one album fetch per
    candidate."""
    owned = []
    for album in albums:
        principal = (album.get("artist") or {}).get("id")
        if principal is None and album.get("source", "deezer") == "deezer":
            try:
                detail = await catalog.deezer.get_album(album["id"])
            except DeezerNotFound:
                continue
            except DeezerError as exc:
                log.warning("deezer album %s could not be read: %s", album["id"], exc)
                continue
            principal = (detail.get("artist") or {}).get("id")
        if principal is not None and principal != owner_id:
            log.info("dropping %r — principal artist %s is not %s",
                     album["title"], principal, owner_id)
            continue
        owned.append(album)
    return owned


async def note_album_variants(session, artist_id: int,
                              sightings: list[variants.Sighting]) -> int:
    """Attach each noticed mix to the artist's release its title names. No
    matching release, no note: a variant is a footnote on a record we already
    track, never a reason to invent one."""
    if not sightings:
        return 0
    releases = (await session.execute(
        select(Release.id, Release.title).where(Release.artist_id == artist_id)
    )).all()
    if not releases:
        return 0
    recorded = 0
    for sighting in sightings:
        # album_score strips a '(Remastered)' qualifier only from its first
        # argument, and here either side can carry one: a catalogue files the
        # Atmos mix under 'Load (Remastered)' while our row, born on Deezer, is
        # 'Load'. Ties go to the older row, so the note lands in the same place
        # every run.
        score, _, release_id = max(
            (max(album_score(title, sighting.title),
                 album_score(sighting.title, title)), -release_id, release_id)
            for release_id, title in releases
        )
        if score < ALBUM_MATCH_THRESHOLD:
            continue
        await variants.note(session, release_id, sighting)
        recorded += 1
        log.info("%s variant of %r noticed on %s (%s)", sighting.variant,
                 sighting.title, sighting.source, sighting.external_id)
    return recorded


async def _populate_discography(session, catalog: CatalogClient,
                                discogs: DiscogsClient | None, artist: Artist,
                                external: dict[str, str]):
    """Baseline release list for a freshly-created artist, by ID (never by
    name): the most trusted catalogue that knows the artist, else the
    Discogs masters. Tracks are NOT fetched here — the folder match pulls the
    tracklist only for the album it needs."""
    source, catalog_id = catalogs.held_by(artist)
    if catalog_id is not None:
        try:
            albums = await catalog.get_artist_albums(catalog_id, source)
        except Exception as exc:
            log.warning("%s albums of artist %s could not be read: %s",
                        source, catalog_id, exc)
            albums = []
        albums = await _owned_albums(catalog, albums, catalog_id)
        sightings = []
        for album in albums:
            # the Atmos edition rides the artist page as its own album under
            # the same title — a row for it would be a phantom second album,
            # so it is noted beside the stereo one instead
            sighting = variants.from_catalog(album)
            if sighting is not None:
                sightings.append(sighting)
                continue
            id_field = catalogs.id_field(album.get("source", source))
            exists = (await session.execute(
                select(Release.id).where(getattr(Release, id_field) == album["id"])
            )).scalar_one_or_none()
            if exists is None:
                session.add(Release(
                    artist_id=artist.id, title=album["title"],
                    record_type=album.get("record_type"),
                    cover_url=album.get("cover_medium"),
                    release_date=album.get("release_date"),
                    **{id_field: album["id"]}))
        await session.flush()
        await note_album_variants(session, artist.id, sightings)
        return
    if discogs is not None and external.get("discogs"):
        try:
            masters = await discogs.cached_artist_masters(session, int(external["discogs"]))
        except DiscogsError as exc:
            log.warning("discogs masters of artist %s could not be read: %s",
                        external["discogs"], exc)
            return
        taken = {row[0] for row in (await session.execute(
            select(Release.discogs_id).where(
                Release.discogs_id.in_([m["id"] for m in masters]))
        )).all()}
        for master in masters:
            if master["id"] in taken:
                continue
            taken.add(master["id"])
            session.add(Release(
                artist_id=artist.id, title=master["title"], discogs_id=master["id"],
                release_date=str(master["year"]) if master.get("year") else None,
                record_type="single" if " / " in master["title"] else None,
                cover_url=master.get("thumb") or None))


async def resolve_artist(session, catalog: CatalogClient,
                         discogs: DiscogsClient | None, name: str,
                         tag_album: str, songs: list[str]) -> Artist | None:
    """Phase 2: find or create the artist AND give it a baseline discography —
    the catalog/Discogs creation paths already populate releases; a Wikidata
    resolution carries only ids, so fill the release list from them here (once
    per artist, never per folder). `songs` are the folder's actual track titles,
    used to corroborate a homonym by content, not just by artist name."""
    artist = await _find_or_create_artist(session, catalog, discogs, name, tag_album, songs)
    if artist is None:
        return None
    has_releases = (await session.execute(
        select(Release.id).where(Release.artist_id == artist.id).limit(1)
    )).scalar_one_or_none() is not None
    if not has_releases:
        external = {
            r.source: r.external_id for r in (await session.execute(
                select(ArtistExternalId).where(ArtistExternalId.artist_id == artist.id)
            )).scalars()
        }
        await _populate_discography(session, catalog, discogs, artist, external)
        await session.commit()
    return artist


async def _create_artist_from_wikidata(session, deezer: DeezerClient,
                                       name: str, tag_album: str) -> Artist | None:
    """Wikidata resolves identity cheaply — no Discogs 60/min catalog walk.
    Confirm the artist (an exact-name musical act; homonyms are disambiguated
    by the folder's album), store the QID and the service ids its claims carry,
    and stop there: the album fallback fetches ONLY the needed album by those
    ids afterwards, never the whole catalog."""
    client = wd.WikidataClient()
    try:
        act = await _wikidata_act(client, name, tag_album)
        if act is None:
            return None
        return await _artist_for_wikidata_act(session, deezer, name, *act)
    finally:
        await client.close()


async def _wikidata_act(client: wd.WikidataClient, name: str,
                        tag_album: str) -> tuple[str, dict] | None:
    candidates = [c for c in await client.search(name)
                  if same_artist(c["label"], name)
                  or (c["match"] and same_artist(c["match"], name))]
    entities = await client.get_entities([c["qid"] for c in candidates[:5]])
    musical = [c for c in candidates[:5]
               if wd.is_musical_artist(entities.get(c["qid"], {}))]
    if len(musical) == 1:
        return musical[0]["qid"], entities[musical[0]["qid"]]
    # homonyms (two bands/singers with the same name): the folder's
    # album picks the right one
    for cand in musical:
        albums = await client.albums_by_performer(cand["qid"])
        if any(album_score(a["label"] or "", tag_album) >= ALBUM_MATCH_THRESHOLD
               for a in albums):
            return cand["qid"], entities[cand["qid"]]
    return None


async def _artist_for_wikidata_act(session, deezer: DeezerClient, name: str,
                                   qid: str, entity: dict) -> Artist:
    # the QID may already own a row (a prior enrich, or a concurrent
    # spelling of the same artist) — adopt it, never collide on the unique
    # wikidata_id
    existing = (await session.execute(
        select(Artist).where(Artist.wikidata_id == qid)
    )).scalar_one_or_none()
    if existing is not None:
        return await _monitored(session, existing)
    deezer_id = await deezer_module.verified_claim_id(
        deezer, wd.first_claim(entity, "P2722"),
        wd.entity_display_name(entity) or name)
    if deezer_id is not None:
        taken = (await session.execute(
            select(Artist).where(Artist.deezer_id == deezer_id)
        )).scalar_one_or_none()
        if taken is not None:
            # the Deezer identity already owns a row — that IS this artist;
            # attach the QID + ids to it instead of forking a QID-only shadow
            if taken.wikidata_id is None:
                taken.wikidata_id = qid
            taken.monitored = True
            await _store_wikidata_ids(session, taken.id, entity)
            await session.commit()
            return taken
    artist = Artist(
        name=wd.entity_display_name(entity) or name,
        wikidata_id=qid,
        deezer_id=deezer_id,
    )
    session.add(artist)
    await session.flush()
    created_artist_ids.add(artist.id)
    await _store_wikidata_ids(session, artist.id, entity)
    await session.commit()
    log.info("artist %r created from Wikidata (%s)", artist.name, qid)
    return artist


async def _store_wikidata_ids(session, artist_id: int, entity: dict):
    for source, prop in (("spotify", "P1902"), ("discogs", "P1953")):
        value = wd.first_claim(entity, prop)
        if value:
            await session.execute(
                insert(ArtistExternalId)
                .values(artist_id=artist_id, source=source, external_id=str(value))
                .on_conflict_do_nothing())


async def _monitored(session, artist: Artist) -> Artist:
    if not artist.monitored:
        artist.monitored = True
        await session.commit()
    return artist


async def _create_artist_from_discogs(session, discogs: DiscogsClient,
                                      name: str, tag_album: str,
                                      songs: list[str] | None = None) -> Artist | None:
    """The last resort, and the loosest: Discogs carries the Jugoton-era names,
    but a generic compilation title ('The Definitive Collection') matches almost
    any artist — so a candidate is trusted only when the folder's actual SONGS
    turn up in its matching master, never on the album title alone."""
    try:
        discogs_id, masters = None, []
        for candidate_id in await discogs.find_artist_ids(name):
            candidate_masters = await discogs.cached_artist_masters(session, candidate_id)
            title_matches = [m for m in candidate_masters
                             if album_score(m["title"], tag_album) >= ALBUM_MATCH_THRESHOLD]
            if not title_matches:
                continue
            if songs:
                corroborated = False
                for master in title_matches[:2]:
                    try:
                        tracks = await discogs.master_tracklist(master["id"])
                    except DiscogsError as exc:
                        log.warning("discogs tracklist of master %s could not be read: %s",
                                    master["id"], exc)
                        continue
                    if matching.songs_overlap([t["title"] for t in tracks],
                                              songs) >= max(2, len(songs) // 3):
                        corroborated = True
                        break
                if not corroborated:
                    continue
            discogs_id, masters = candidate_id, candidate_masters
            break
        if discogs_id is None:
            return None
        info = await discogs.get_artist(discogs_id)
    except DiscogsError as exc:
        log.error("discogs artist fallback failed for %s: %s", name, exc)
        return None

    artist = Artist(name=info.get("name") or name)
    session.add(artist)
    await session.flush()
    created_artist_ids.add(artist.id)
    await session.execute(
        insert(ArtistExternalId)
        .values(artist_id=artist.id, source="discogs", external_id=str(discogs_id))
        .on_conflict_do_nothing())
    taken = {
        row[0] for row in (await session.execute(
            select(Release.discogs_id).where(
                Release.discogs_id.in_([m["id"] for m in masters])
            )
        )).all()
    }
    for master in masters:
        if master["id"] in taken:
            continue
        taken.add(master["id"])
        session.add(Release(
            artist_id=artist.id,
            title=master["title"],
            discogs_id=master["id"],
            release_date=str(master["year"]) if master.get("year") else None,
            record_type="single" if " / " in master["title"] else None,
            cover_url=master.get("thumb") or None,
        ))
    await session.commit()
    log.info("artist %r created from Discogs (%d masters)", artist.name, len(masters))
    return artist


async def find_artist_db(session, name: str,
                         tag_album: str | None = None) -> Artist | None:
    """BEST match above the trusted score — never the first row that clears
    it: 'Jame$ Brown' folds to 95 against 'James Brown' and, taken first,
    swallows the whole catalog of the real artist. A fuzzy-only winner (one
    that is not `same_artist`) must corroborate with the folder's album in
    its catalog: 'Animal' scores 92.3 against 'The Animals' and is a
    different band. No corroboration means no match here — the catalog
    search that follows adopts the right row by id anyway."""
    result = await session.execute(select(Artist))
    best, best_key = None, (False, 0.0)
    for artist in result.scalars():
        score = _artist_name_score(name, artist.name)
        if score < ARTIST_TRUSTED_SCORE:
            continue
        key = (same_artist(artist.name, name), score)
        if key > best_key:
            best, best_key = artist, key
    if best is None:
        return None
    if not best_key[0]:
        titles = (await session.execute(
            select(Release.title).where(Release.artist_id == best.id)
        )).scalars().all()
        if not tag_album or not any(
                album_score(title, tag_album) >= ALBUM_MATCH_THRESHOLD
                for title in titles):
            log.info("fuzzy artist match %r -> %r (%.1f) rejected: its catalog "
                     "does not carry %r", name, best.name, best_key[1], tag_album)
            return None
    if not best.monitored:
        best.monitored = True
    return best


async def adopt_catalog_ids(session, catalog: CatalogClient, artist: Artist):
    """One-time lift of an existing (Deezer-born) artist onto each catalogue
    trusted above Deezer: find its entity by name, adopt its id and its cleaner
    name (BRANIMIR ŠTULIĆ -> Branimir Štulić). Skipped once the id is set; an id
    already claimed by another row (Deezer's split '& Majke'/'& Plaćenici' pages
    collapse to one artist there) is left for a deliberate merge, never a
    unique-constraint crash."""
    # search by the clean name so a Wikipedia-style qualifier ('Đavoli
    # (glazbeni sastav)') doesn't sink the match below the trusted score — then
    # adopt the qualifier-free catalogue name in its place
    query = wd.strip_disambiguator(artist.name)
    for source, client in catalog.primary.items():
        column = catalogs.id_field(source)
        if getattr(artist, column) is not None:
            continue
        try:
            hits = await client.search_artists(query)
        except Exception as exc:
            log.debug("%s id search failed for %s: %s", source, query, exc)
            continue
        # identity adoption must be EXACT — a 90+ fuzzy neighbour is how a ghost
        # gets an id ("Brothers In Blues" clears the trusted score for "Blues
        # Brothers" while being a different band)
        ranked = sorted(
            (h for h in hits
             if (h.get("nb_album") or 0) > 0 and same_artist(h["name"], query)),
            key=lambda h: h.get("nb_album") or 0,
            reverse=True,
        )
        if not ranked:
            continue
        best = ranked[0]
        taken = (await session.execute(
            select(Artist.id).where(getattr(Artist, column) == best["id"], Artist.id != artist.id)
        )).scalar_one_or_none()
        if taken is not None:
            continue
        setattr(artist, column, best["id"])
        clean = clean_display_name(best["name"])
        if same_artist(clean, query) and clean != artist.name:
            artist.name = clean


async def _album_corroborates_songs(catalog, albums: list[dict], source: str,
                                    songs: list[str]) -> bool:
    """A matching album TITLE is not enough for a homonym ('The Definitive
    Collection' exists for every artist) — pull the album's tracklist and demand
    the folder's own songs actually appear in it."""
    threshold = max(2, len(songs) // 3)
    for album in albums[:2]:
        try:
            tracks = await catalog.get_album_tracks(album["id"], source)
        except Exception as exc:
            log.warning("%s tracklist of album %s could not be read: %s",
                        source, album["id"], exc)
            continue
        if matching.songs_overlap([t["title"] for t in tracks], songs) >= threshold:
            return True
    return False


async def _find_or_create_artist(session, catalog: CatalogClient,
                                 discogs: DiscogsClient | None, name: str,
                                 tag_album: str | None = None,
                                 songs: list[str] | None = None) -> Artist | None:
    artist = await find_artist_db(session, name, tag_album)
    if artist is not None:
        return artist
    best, albums = await _trusted_catalog_hit(catalog, name, tag_album, songs)
    if best is None:
        # the catalog has nothing trustworthy — Wikidata resolves the identity
        # cheaply (and hands us the ids), then Discogs as the last resort
        if not tag_album:
            return None
        via_wd = await _create_artist_from_wikidata(session, catalog.deezer, name, tag_album)
        if via_wd is not None:
            return via_wd
        if discogs is not None:
            return await _create_artist_from_discogs(session, discogs, name, tag_album, songs)
        return None
    id_field = catalogs.id_field(best.get("source", "deezer"))
    adopted = await _adopt_catalog_artist(session, best, id_field)
    if adopted is not None:
        return adopted
    return await _create_catalog_artist(session, catalog, best, albums, id_field)


def _ranked_catalog_hits(name: str, hits: list[dict]) -> list[dict]:
    # ghost/duplicate pages carry nb_album=0 (a catalogue without album counts
    # maps its popularity here, so a live page still outranks the noise) — never candidates: they would win the
    # ranking and then fail everything ("Dedić Arsen" vs "Arsen Dedic")
    candidates = [
        h for h in hits
        if (h.get("nb_album") or 0) > 0
        and _artist_name_score(name, h["name"]) >= ARTIST_MATCH_THRESHOLD
    ]
    return sorted(candidates, key=lambda h: (
        same_artist(h["name"], name),
        _artist_name_score(name, h["name"]),
        h.get("nb_album") or 0,
    ), reverse=True)


async def _trusted_catalog_hit(catalog: CatalogClient, name: str,
                               tag_album: str | None, songs: list[str] | None
                               ) -> tuple[dict | None, list[dict] | None]:
    ranked = _ranked_catalog_hits(name, await catalog.search_artists(name))
    exact_matches = sum(1 for h in ranked if same_artist(h["name"], name))
    for hit in ranked[:6]:
        # only an exact (folded) name is trusted on its own — a high SCORE is
        # not: token_sort ignores word order, so "Brothers In Blues" clears the
        # trusted bar against "Blues Brothers" while being a different band.
        # Non-exact hits, and one exact name among several (homonyms: fifteen
        # pages called "Cream", the Turkish "Azra"), must be corroborated by
        # the folder's album before we trust the page
        needs_validation = (not same_artist(hit["name"], name)) or exact_matches > 1
        fetched = await catalog.get_artist_albums(hit["id"], hit["source"])
        if needs_validation and not await _page_corroborated(
                catalog, fetched, hit["source"], tag_album, songs):
            continue
        return hit, fetched
    return None, None


async def _page_corroborated(catalog: CatalogClient, albums: list[dict], source: str,
                             tag_album: str | None, songs: list[str] | None) -> bool:
    title_matches = [
        a for a in albums
        if tag_album and album_score(a["title"], tag_album) >= ALBUM_MATCH_THRESHOLD
    ]
    if not title_matches:
        return False
    # the album title alone is too weak for a homonym — ground the
    # decision in the folder's actual songs when we have them
    return not songs or await _album_corroborates_songs(catalog, title_matches, source, songs)


async def _adopt_catalog_artist(session, best: dict, id_field: str) -> Artist | None:
    # the page may already be in the DB under a name the fuzzy loop missed
    # (enrichment shadow rows, differently-spelled tags): adopt, don't duplicate
    existing = (await session.execute(
        select(Artist).where(getattr(Artist, id_field) == best["id"])
    )).scalar_one_or_none()
    if existing is not None:
        return await _monitored(session, existing)
    # the act may already live in the DB under the OTHER source's id (a
    # Deezer-born row surfacing through a search above Deezer): adopt it by exact
    # name and fill the missing id column instead of forking a second row
    for cand in (await session.execute(
            select(Artist).order_by(Artist.id))).scalars():
        if same_artist(cand.name, best["name"]) and getattr(cand, id_field) is None:
            setattr(cand, id_field, best["id"])
            cand.monitored = True
            await session.commit()
            return cand
    return None


async def _create_catalog_artist(session, catalog: CatalogClient, best: dict,
                                 albums: list[dict], id_field: str) -> Artist:
    artist = Artist(
        name=clean_display_name(best["name"]),
        image_url=best.get("picture_medium"),
        **{id_field: best["id"]},
    )
    session.add(artist)
    await session.flush()
    created_artist_ids.add(artist.id)
    # a catalog lists shared compilations under every contributing artist — an
    # album already present (under whichever artist, or twice in this very
    # payload) must not be re-inserted
    albums = await _owned_albums(catalog, albums, best["id"])
    rel_col = getattr(Release, id_field)
    taken = {
        row[0] for row in (await session.execute(
            select(rel_col).where(rel_col.in_([a["id"] for a in albums]))
        )).all()
    }
    sightings = []
    for album in albums:
        if album["id"] in taken:
            continue
        sighting = variants.from_catalog(album)
        if sighting is not None:
            sightings.append(sighting)
            continue
        taken.add(album["id"])
        session.add(Release(
            artist_id=artist.id,
            title=album["title"],
            release_date=album.get("release_date"),
            record_type=album.get("record_type"),
            cover_url=album.get("cover_medium"),
            **{id_field: album["id"]},
        ))
    await session.flush()
    await note_album_variants(session, artist.id, sightings)
    # persist the artist NOW, independent of this folder's outcome — a later
    # album_not_found must not roll the discography back (and the post-scan
    # enricher needs the row to exist)
    await session.commit()
    # no per-artist enrich here: the batch enricher runs after the scan
    return artist
