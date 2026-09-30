"""Artist enrichment: resolve the Wikidata QID, then pull external IDs, the
Wikipedia bio (hr preferred, en fallback), lifetime, country, portrait and
group-membership relations. An artist that cannot be resolved exactly stays
'unresolved' for manual resolution in the UI — never guessed."""

import logging
from dataclasses import dataclass

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from opus.db import SessionLocal
from opus.music.metadata import deezer, discography
from opus.music.metadata import wikidata as wd
from opus.music.metadata import wikitext as wt
from opus.music.metadata.musicbrainz import MusicBrainzClient, MusicBrainzError
from opus.music.metadata.deezer import DeezerClient
from opus.models import (ARTIST_CATALOG_IDS, Artist, ArtistExternalId, ArtistRelation, EnrichStatus,
                         Image, Release)
from opus import passes
from opus.music.textnorm import album_score, latin, same_artist

log = logging.getLogger("opus.enrich")

def spawn_enrich(artist_id: int, qid: str | None = None):
    passes.spawn(enrich_artist(artist_id, qid))


def year_of_record(release_date: str | None) -> int | None:
    if not release_date or len(release_date) < 4 or not release_date[:4].isdigit():
        return None
    year = int(release_date[:4])
    return year if 1850 <= year <= 2100 else None


async def _year_of_first_record(session, artist_id: int) -> int | None:
    """The last resort, and the only one that cannot come back empty for an
    artist who has music: an act nobody wrote an article about still made a
    first record, and the year of it is near enough the year they appeared."""
    dates = (await session.execute(
        select(Release.release_date).where(Release.artist_id == artist_id)
    )).scalars()
    years = [y for y in (year_of_record(d) for d in dates) if y is not None]
    return min(years) if years else None


async def _year_from_article(client, sitelinks: dict, langs, person: bool) -> int | None:
    """The year off the artist's own Wikipedia article, for the acts Wikidata
    describes without ever dating them."""
    for lang in langs:
        sitelink = sitelinks.get(f"{lang}wiki")
        if not sitelink:
            continue
        try:
            markup = await client.wikipedia_wikitext(lang, sitelink["title"])
        except Exception as exc:
            log.error("wikipedia wikitext failed for %s: %s", sitelink["title"], exc)
            continue
        year = wt.begin_year(latin(markup), person=person) if markup else None
        if year is not None:
            log.info("begin year %d read off %s.wikipedia %s",
                     year, lang, sitelink["title"])
            return year
    return None


async def _year_by_name(client, name: str) -> int | None:
    """A year for an artist Wikidata cannot resolve at all. Only an article
    TITLED with their name counts, and only one that is about music — a
    same-named footballer must not lend anybody a year."""
    if not name:
        return None
    for lang in ("hr", "sr", "en"):
        try:
            titles = await client.wikipedia_titles_named(lang, name)
        except Exception as exc:
            log.error("wikipedia search failed for %s: %s", name, exc)
            continue
        for title in titles:
            try:
                markup = await client.wikipedia_wikitext(lang, title)
            except Exception as exc:
                log.error("wikipedia wikitext failed for %s: %s", title, exc)
                continue
            markup = latin(markup)
            if not markup or not wt.about_music(markup):
                continue
            year = wt.begin_year(markup) or wt.year_in_prose(markup)
            if year is not None:
                log.info("begin year %d read off %s.wikipedia %s (unresolved artist)",
                         year, lang, title)
                return year
    return None


async def enrich_artist(artist_id: int, qid: str | None = None,
                        chain_discography: bool = True):
    client = wd.WikidataClient()
    try:
        await _enrich(artist_id, qid, client, chain_discography)
    except Exception:
        log.exception("enrichment failed for artist %s", artist_id)
        async with SessionLocal() as session:
            artist = await session.get(Artist, artist_id)
            if artist is not None:
                artist.enrich_status = EnrichStatus.FAILED
                await session.commit()
    finally:
        await client.close()


async def _resolve_by_discography(client: wd.WikidataClient,
                                  artist_id: int) -> str | None:
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        name = artist.name
        titles = [r.title for r in (await session.execute(
            select(Release).where(Release.artist_id == artist_id)
        )).scalars()][:20]
    candidates = [c for c in await client.search(name)
                  if same_artist(c["label"], name)
                  or (c["match"] and same_artist(c["match"], name))]
    if not candidates:
        return None
    entities = await client.get_entities([c["qid"] for c in candidates[:5]])
    musical = [c for c in candidates[:5]
               if wd.is_musical_artist(entities.get(c["qid"], {}))]
    # a single exact-name musical entity IS the artist — trust it even when
    # Wikidata's discography is too sparse to corroborate our albums (Belfast
    # Food: exact name, 'Croatian music band', barely any albums listed)
    if len(musical) == 1:
        log.info("artist %s resolved by unique exact name: %s (%s)",
                 artist_id, musical[0]["qid"], musical[0]["label"])
        return musical[0]["qid"]
    # homonyms — require the candidate's own Wikidata discography to match ours
    for candidate in musical[:3]:
        albums = await client.albums_by_performer(candidate["qid"])
        if any(album_score(album["label"] or "", title) >= 85
               for album in albums for title in titles):
            log.info("artist %s resolved by name+discography: %s (%s)",
                     artist_id, candidate["qid"], candidate["label"])
            return candidate["qid"]
    return None


async def _deezer_id_by_name(name: str) -> int | None:
    """No P2722 claim and no id from the scan: adopt the Deezer page whose
    name IS the artist (exact same_artist, largest catalog on ties). Keeps the
    canonical catalog reachable for artists born in a catalogue above Deezer
    whose spelling there differs from Deezer's ('Vojko Vrućina' vs 'Vojko V')."""
    client = DeezerClient()
    try:
        hits = await client.search_artists(name)
    except Exception as exc:
        log.error("deezer id backfill search failed for %r: %s", name, exc)
        return None
    finally:
        await client.close()
    ranked = sorted(
        (h for h in hits
         if (h.get("nb_album") or 0) > 0 and same_artist(h.get("name") or "", name)),
        key=lambda h: h.get("nb_album") or 0,
        reverse=True,
    )
    return int(ranked[0]["id"]) if ranked else None


async def _absorb_qid_holder(session, artist: Artist, qid: str):
    """Two rows resolved to one QID are one act — a relation shadow or an
    import fork from before alias matching. The enriched row survives: the
    holder's catalog rows, ids and relations move onto it (files follow their
    releases), and the chained discography sync collapses fold-equal release
    duplicates. Nothing is deleted that anything still holds."""
    holder = (await session.execute(
        select(Artist).where(Artist.wikidata_id == qid, Artist.id != artist.id)
    )).scalar_one_or_none()
    if holder is None:
        return
    log.warning("artist %s absorbs duplicate artist %s (%r) holding %s",
                artist.id, holder.id, holder.name, qid)
    moved = {column: getattr(holder, column) for column in ARTIST_CATALOG_IDS}
    holder.wikidata_id = None
    for column in moved:
        setattr(holder, column, None)
    await session.flush()
    for column, value in moved.items():
        if getattr(artist, column) is None:
            setattr(artist, column, value)
    artist.monitored = artist.monitored or holder.monitored
    await session.execute(
        update(Release).where(Release.artist_id == holder.id)
        .values(artist_id=artist.id)
    )
    relations = [
        (rel.artist_id, rel.related_artist_id, rel.relation, rel.source)
        for rel in (await session.execute(
            select(ArtistRelation).where(or_(
                ArtistRelation.artist_id == holder.id,
                ArtistRelation.related_artist_id == holder.id,
            ))
        )).scalars()
    ]
    await session.execute(delete(ArtistRelation).where(or_(
        ArtistRelation.artist_id == holder.id,
        ArtistRelation.related_artist_id == holder.id,
    )))
    await session.flush()
    for a, b, relation, source in relations:
        a = artist.id if a == holder.id else a
        b = artist.id if b == holder.id else b
        if a == b:
            continue
        await session.execute(
            insert(ArtistRelation)
            .values(artist_id=a, related_artist_id=b, relation=relation, source=source)
            .on_conflict_do_nothing()
        )
    await session.execute(delete(ArtistExternalId)
                          .where(ArtistExternalId.artist_id == holder.id))
    await session.execute(delete(Image).where(Image.entity_type == "artist",
                                              Image.entity_id == holder.id))
    await session.delete(holder)
    await session.flush()


@dataclass
class _Facts:
    """What Wikidata, Wikipedia and Deezer say about one resolved artist, read
    before anything is written."""

    qid: str
    person: bool
    begin_year: int | None
    end_year: int | None
    related_qids: list[str]
    related_entities: dict
    langs: tuple[str, str]
    country: str | None
    country_hr: str | None
    bio: str | None
    bio_url: str | None
    articles: list[tuple[str, str]]
    portrait_url: str | None
    external_ids: dict[str, str]
    wikidata_deezer: int | None
    stored_verified: bool
    studio_albums: list[str] | None
    canonical_name: str | None
    deezer_backfill: int | None


async def _enrich(artist_id: int, qid: str | None, client: wd.WikidataClient,
                  chain_discography: bool = True):
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        if artist is None:
            log.error("enrich: artist %s not found", artist_id)
            return
        deezer_id = artist.deezer_id
        if qid is None:
            qid = artist.wikidata_id

    if qid is None and deezer_id is not None:
        qid = await client.qid_by_deezer_id(deezer_id)

    if qid is None:
        # not guessing — an exact-name candidate counts only when its own
        # Wikidata discography corroborates albums we actually hold (the
        # only automatic path for Discogs-born artists without a Deezer id)
        qid = await _resolve_by_discography(client, artist_id)

    if qid is None:
        await _leave_unresolved(client, artist_id)
        return

    facts = await _facts(client, qid, deezer_id)
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        await _absorb_qid_holder(session, artist, qid)
        artist.wikidata_id = qid
        await _write_identity(session, artist, facts)
        await _write_portrait_and_ids(session, artist, facts)
        await _write_relations(session, client, artist, facts)
        artist.enrich_status = EnrichStatus.RESOLVED
        await session.commit()
    log.info("artist %s enriched from %s (%d relations, %d external ids)",
             artist_id, qid, len(facts.related_qids), len(facts.external_ids))
    # enrichment brought the external IDs — now merge the secondary catalogs
    # (the batch enricher sequences this itself)
    if chain_discography:
        discography.spawn_sync(artist_id)


async def _leave_unresolved(client, artist_id: int) -> None:
    """Unresolved is not the same as undated: the article carries the year even
    when nothing links it to a Wikidata item."""
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        name = artist.name
        missing_year = artist.begin_year is None
    year = await _year_by_name(client, name) if missing_year else None
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        artist.enrich_status = EnrichStatus.UNRESOLVED
        if artist.begin_year is None:
            artist.begin_year = year or await _year_of_first_record(session, artist_id)
        # nothing in Wikidata answers to this name, which is no reason for
        # nobody to be asked: MusicBrainz keeps groups Wikidata never heard of
        await session.execute(
            delete(ArtistRelation).where(
                ArtistRelation.relation == "member_of",
                ArtistRelation.source == "musicbrainz",
                ArtistRelation.related_artist_id == artist_id))
        await _line_up_from_musicbrainz(session, client, artist_id, name, None, None, False)
        await session.commit()
    log.info("artist %s: no exact Wikidata match, left unresolved", artist_id)


async def _facts(client, qid: str, deezer_id: int | None) -> _Facts:
    entities = await client.get_entities([qid])
    entity = entities.get(qid)
    if entity is None or "missing" in entity:
        raise wd.WikidataError(f"entity {qid} not found")

    person = wd.is_person(entity)
    begin_year, end_year, country_qids, related_qids = await _origin(client, entity, person)
    related_entities = await client.get_entities(related_qids)

    # info language: English by default, Croatian for Croatian artists
    langs = ("hr", "en") if wd.CROATIA_QID in country_qids else ("en", "hr")
    country, country_hr = await _country(client, country_qids)

    articles = [(lang, sitelinks_of["title"])
                for lang in langs
                if (sitelinks_of := entity.get("sitelinks", {}).get(f"{lang}wiki"))]
    sitelinks = entity.get("sitelinks", {})
    bio, bio_url = await _bio(client, sitelinks, langs)

    if begin_year is None:
        # an item can be resolved and still link to no article, or link to one
        # that never states a year — the search by name is asked either way
        begin_year = (await _year_from_article(client, sitelinks, langs, person)
                      or (wt.year_in_prose(bio, person) if bio else None)
                      or await _year_by_name(
                          client, wd.entity_display_name(entity, preferred=langs) or ""))

    portrait_url = wd.commons_image_url(entity)
    external_ids = _external_ids(entity)
    wikidata_deezer, stored_verified = await _deezer_claims(
        entity, wd.entity_display_name(entity, preferred=langs) or "", deezer_id)
    studio_albums = await _studio_albums(client, qid)
    canonical_name = wd.entity_display_name(entity, preferred=langs)

    deezer_backfill = None
    if deezer_id is None and wikidata_deezer is None and canonical_name:
        deezer_backfill = await _deezer_id_by_name(canonical_name)

    return _Facts(
        qid=qid, person=person, begin_year=begin_year, end_year=end_year,
        related_qids=related_qids, related_entities=related_entities, langs=langs,
        country=country, country_hr=country_hr, bio=bio, bio_url=bio_url, articles=articles,
        portrait_url=portrait_url, external_ids=external_ids,
        wikidata_deezer=wikidata_deezer, stored_verified=stored_verified,
        studio_albums=studio_albums, canonical_name=canonical_name,
        deezer_backfill=deezer_backfill)


async def _country(client, country_qids: list[str]) -> tuple[str | None, str | None]:
    if not country_qids:
        return None, None
    country_entities = await client.get_entities(country_qids[:1])
    country_entity = country_entities.get(country_qids[0])
    if not country_entity:
        return None, None
    return (wd.entity_label(country_entity, preferred=("en", "hr")),
            (country_entity.get("labels", {}).get("hr") or {}).get("value"))


def _external_ids(entity: dict) -> dict[str, str]:
    return {
        source: str(wd.first_claim(entity, prop))
        for prop, source in wd.EXTERNAL_ID_PROPS.items()
        if source != "deezer" and wd.first_claim(entity, prop) is not None
    }


async def _deezer_claims(entity: dict, entity_name: str,
                         deezer_id: int | None) -> tuple[int | None, bool]:
    """The Deezer id Wikidata claims, once its page is seen to carry the name,
    and whether the id already stored passes the same test."""
    deezer_client = DeezerClient()
    try:
        wikidata_deezer = await deezer.verified_claim_id(
            deezer_client, wd.first_claim(entity, "P2722"), entity_name)
        stored_verified = deezer_id is not None and await deezer.verified_claim_id(
            deezer_client, deezer_id, entity_name) is not None
    finally:
        await deezer_client.close()
    return wikidata_deezer, stored_verified


async def _studio_albums(client, qid: str) -> list[str] | None:
    try:
        return await client.artist_studio_albums(qid)
    except Exception as exc:
        log.error("studio-albums list failed for %s: %s", qid, exc)
        return None


async def _origin(client, entity: dict, person: bool
                  ) -> tuple[int | None, int | None, list[str], list[str]]:
    """When the artist began and ended, where they come from, and who they are
    bound up with — read the way round that fits a person or a group."""
    if person:
        # born, and failing that when they started working — a session player
        # with no date of birth on record still has a decade they belong to
        begin_year = wd.claim_year(entity, "P569") or wd.claim_year(entity, "P2031")
        end_year = wd.claim_year(entity, "P570")
        country_qids = wd.current_item_ids(entity, "P27")
        related_qids = wd.claim_item_ids(entity, "P463")  # groups this person is in
    else:
        # formed, then active from, then born — the last because a stage name
        # filed as a band is still one person with a date on them
        begin_year = (wd.claim_year(entity, "P571")
                      or wd.claim_year(entity, "P2031")
                      or wd.claim_year(entity, "P580")
                      or wd.claim_year(entity, "P569"))
        end_year = wd.claim_year(entity, "P576")
        country_qids = wd.current_item_ids(entity, "P495")
        related_qids = wd.claim_item_ids(entity, "P527")  # members of this group
        # country of origin is often the state at formation (SFR Yugoslavia);
        # the modern country of the formation location (Zagreb -> Croatia) is
        # what a listener expects
        formation_loc = wd.claim_item_ids(entity, "P740")
        if formation_loc:
            loc_entities = await client.get_entities(formation_loc[:1])
            loc = loc_entities.get(formation_loc[0])
            modern = wd.current_item_ids(loc, "P17") if loc else []
            if modern:
                country_qids = modern + country_qids

    # a dissolved state (SFR Yugoslavia) yields to a modern successor whenever
    # Wikidata lists both — with or without a formation-location claim
    modern_qids = [q for q in country_qids if q not in wd.DISSOLVED_STATES]
    if modern_qids:
        country_qids = modern_qids
    return begin_year, end_year, country_qids, related_qids


async def _bio(client, sitelinks: dict, langs: tuple[str, str]) -> tuple[str | None, str | None]:
    for lang in langs:
        sitelink = sitelinks.get(f"{lang}wiki")
        if not sitelink:
            continue
        article = await client.wikipedia_extract(lang, sitelink["title"])
        if article:
            return article["extract"], article["url"]
    return None, None


async def _write_identity(session, artist: Artist, facts: _Facts) -> None:
    artist_id = artist.id
    canonical_name = facts.canonical_name
    if canonical_name:
        current = artist.name or ""
        # Deezer is canonical: keep its name (Jura Stublić, not the hr
        # label Jurislav Stublić). The Wikidata label wins only when the
        # Deezer name is non-Latin (ABBA 'ابا') or the same name whose
        # current casing is visibly broken — ALL-CAPS or all-lowercase
        # (DRUGI NAČIN -> Drugi način). A mixed-case name is never
        # degraded: Wikidata's concept labels are lowercase ('various
        # artists') and must not rewrite 'Various Artists'.
        uncased = current in (current.upper(), current.lower())
        if (artist.deezer_id is None
                or wd.has_non_latin(current)
                or (same_artist(current, canonical_name)
                    and current != canonical_name and uncased)):
            artist.name = canonical_name
    if facts.studio_albums is not None:
        artist.wiki_studio_albums = facts.studio_albums
    artist.artist_type = "person" if facts.person else "group"
    artist.begin_year = facts.begin_year or await _year_of_first_record(session, artist_id)
    artist.end_year = facts.end_year
    artist.country = facts.country
    artist.country_hr = facts.country_hr
    artist.bio = facts.bio
    artist.bio_url = facts.bio_url
    wikidata_deezer = facts.wikidata_deezer
    adopted_deezer = wikidata_deezer if wikidata_deezer is not None else facts.deezer_backfill
    if artist.deezer_id is None and adopted_deezer is not None:
        taken = await session.execute(
            select(Artist.id).where(Artist.deezer_id == int(adopted_deezer))
        )
        if taken.scalar_one_or_none() is None:
            artist.deezer_id = int(adopted_deezer)
    elif (wikidata_deezer is not None
            and artist.deezer_id != int(wikidata_deezer)
            and not facts.stored_verified):
        # the stored page does NOT carry the artist's name while the
        # curated claim's page does: the scan matched a different act
        # ('The Rolling Stoners', 3 fans, for The Rolling Stones). A
        # page-verified claim outranks a scan guess, so take the id and
        # the name that belongs to it — and drop the other catalogues' ids,
        # which the same wrong identity brought in (a scan re-adopts them by
        # name).
        # A stored page carrying the artist's OWN name never lands here:
        # Deezer keeps duplicate pages per act and Wikidata may cite the
        # weaker one.
        taken = await session.execute(
            select(Artist.id).where(Artist.deezer_id == int(wikidata_deezer),
                                    Artist.id != artist_id)
        )
        if taken.scalar_one_or_none() is not None:
            log.warning("artist %s deezer_id %s conflicts with Wikidata claim "
                        "%s on %s, held by another row — review the identity",
                        artist_id, artist.deezer_id, wikidata_deezer, facts.qid)
        else:
            log.warning("artist %s deezer_id %s is a different act — adopting "
                        "the verified claim %s from %s",
                        artist_id, artist.deezer_id, wikidata_deezer, facts.qid)
            for column in ARTIST_CATALOG_IDS:
                setattr(artist, column, None)
            artist.deezer_id = int(wikidata_deezer)
            if canonical_name:
                artist.name = canonical_name


async def _write_portrait_and_ids(session, artist: Artist, facts: _Facts) -> None:
    artist_id = artist.id
    if facts.portrait_url:
        if artist.image_url is None:
            artist.image_url = facts.portrait_url
        await session.execute(
            insert(Image)
            .values(entity_type="artist", entity_id=artist_id,
                    source="wikimedia", url=facts.portrait_url, width=1000)
            .on_conflict_do_nothing()
        )

    await session.execute(
        delete(ArtistExternalId).where(ArtistExternalId.artist_id == artist_id)
    )
    for source, external_id in facts.external_ids.items():
        await session.execute(
            insert(ArtistExternalId)
            .values(artist_id=artist_id, source=source, external_id=external_id)
            .on_conflict_do_nothing()
        )


async def _write_relations(session, client, artist: Artist, facts: _Facts) -> None:
    artist_id, person = artist.id, facts.person
    # what this pass is about to credit replaces what an earlier one did,
    # rather than joining it. Relations were only ever inserted, so a name
    # Wikidata has since dropped — or one an earlier pass took before it
    # checked whether the thing was even a musical act — stayed on the page
    # for good.
    await session.execute(
        delete(ArtistRelation).where(
            ArtistRelation.relation == "member_of",
            # everything this pass authors, which is everything except what
            # a person put there by hand
            ArtistRelation.source != "manual",
            (ArtistRelation.artist_id == artist_id) if person
            else (ArtistRelation.related_artist_id == artist_id))
    )
    await _relations_from_wikidata(session, artist_id, facts)
    if not person:
        await _line_up_from_wikipedia(session, client, artist_id, facts)
    # MusicBrainz keeps membership as a relation of its own, with the years
    # on it, which is why it holds line-ups neither of the others does —
    # forty-six of Black Sabbath against Wikidata's five, and `Mile i
    # Putnici` naming Mile Kekin its founder where nothing else records the
    # group at all. It is asked about people and nothing else: the records
    # still come from Deezer, which is the premise this house was built on.
    await _line_up_from_musicbrainz(
        session, client, artist_id, artist.name,
        facts.external_ids.get("musicbrainz"), facts.qid, person)


async def _relations_from_wikidata(session, artist_id: int, facts: _Facts) -> None:
    person = facts.person
    for related_qid in facts.related_qids:
        related_entity = facts.related_entities.get(related_qid)
        if related_entity is None or "missing" in related_entity:
            continue
        # P463 is "member of" in Wikidata's widest sense, and a career like
        # Dylan's is full of academies and societies. A shelf of artists is
        # not a list of memberships: the Traveling Wilburys belong on it and
        # the American Academy of Arts and Letters does not.
        if not wd.is_musical_artist(related_entity):
            continue
        # A person's own "member of" is the weaker of the two claims, and
        # Wikidata does not require the pair to agree: Dino Dvornik's entry
        # says Bijelo Dugme and Bijelo Dugme's thirteen members do not say
        # him. Where the group publishes a line-up at all, it settles it —
        # where it publishes none, there is nothing to contradict.
        if person:
            line_up = wd.claim_item_ids(related_entity, "P527")
            if line_up and facts.qid not in line_up:
                log.info("artist %s: %s does not list them among its %d members",
                         artist_id, related_qid, len(line_up))
                continue
        related_id = await _get_or_create_related(session, related_qid, related_entity)
        if related_id is None or related_id == artist_id:
            continue
        # member_of always points member -> group
        member_id, group_id = (artist_id, related_id) if person else (related_id, artist_id)
        await session.execute(
            insert(ArtistRelation)
            .values(artist_id=member_id, related_artist_id=group_id,
                    relation="member_of", source="wikidata")
            .on_conflict_do_nothing()
        )


async def _line_up_from_wikipedia(session, client, artist_id: int, facts: _Facts) -> None:
    """Wikidata carries a line-up for some bands and nothing for most — The Beat
    Fleet's entry names not one of its six, and neither does MusicBrainz. The
    article that describes them has both lists in the same infobox this module
    already reads. It supplements rather than replaces: a name is the weakest
    identity here, so anything Wikidata vouched for above already holds its
    place.

    Every article, not the first: the Croatian page for The Beat Fleet carries no
    line-up and the English one carries thirteen."""
    line_up: dict[str, list[str]] = {}
    for lang, title in facts.articles:
        try:
            wikitext = await client.wikipedia_wikitext(lang, title)
        except Exception as exc:
            log.warning("wikipedia line-up failed for %s (%s): %s", facts.qid, lang, exc)
            continue
        line_up = wt.parse_infobox_members(wikitext or "")
        if line_up:
            break
    for member_name in [n for names in line_up.values() for n in names]:
        member_id = await _member_by_name(session, client, member_name, facts.qid)
        if member_id is None or member_id == artist_id:
            continue
        await session.execute(
            insert(ArtistRelation)
            .values(artist_id=member_id, related_artist_id=artist_id,
                    relation="member_of", source="wikipedia")
            .on_conflict_do_nothing()
        )


async def _line_up_from_musicbrainz(session, client, artist_id: int, name: str,
                                    mb_id: str | None, qid: str | None,
                                    person: bool) -> None:
    """Who MusicBrainz says stood in this group, or which groups this person
    stood in.

    Its id usually arrives as a Wikidata claim, and an artist Wikidata has never
    heard of has no claim to arrive on — which is exactly the artist this is
    worth asking about. So where no id is known the name is put to MusicBrainz
    directly: `Mile i Putnici` is nowhere in Wikidata and an exact match here,
    naming Mile Kekin its founder."""
    client_mb = MusicBrainzClient()
    try:
        if not mb_id:
            mb_id = await client_mb.find_artist(name)
            if mb_id:
                await session.execute(
                    insert(ArtistExternalId)
                    .values(artist_id=artist_id, source="musicbrainz", external_id=mb_id)
                    .on_conflict_do_nothing()
                )
        if not mb_id:
            return
        kin = await client_mb.artist_relations(mb_id)
    except MusicBrainzError as exc:
        log.warning("musicbrainz line-up failed for artist %s: %s", artist_id, exc)
        return
    for other in kin:
        # a person names their groups; a group names its people
        if other["person"] == person:
            continue
        other_id = await _member_by_name(session, client, other["name"], qid)
        if other_id is None or other_id == artist_id:
            continue
        member_id, group_id = (artist_id, other_id) if person else (other_id, artist_id)
        await session.execute(
            insert(ArtistRelation)
            .values(artist_id=member_id, related_artist_id=group_id,
                    relation="member_of", source="musicbrainz")
            .on_conflict_do_nothing()
        )


async def _member_by_name(session, client, name: str, band_qid: str | None) -> int | None:
    """The artist this name belongs to, made if nobody answers to it.

    The Wikipedia line-up is names and nothing else, so this is the one relation
    built on a name alone — but a name is where the search starts, not where it
    stops. Somebody named in a band's infobox usually has an entity of their
    own, and taking the name without asking leaves a row with nothing on it: no
    portrait, no dates, and no way of ever learning what else they played on.

    The band settles the homonyms. Where two people answer to a name, the one
    whose own entry names this band is the one who was in it; where none does
    and only one is a musician, that is them."""
    for cand in (await session.execute(select(Artist))).scalars():
        if same_artist(cand.name, name):
            return cand.id

    qid = None
    try:
        hits = [c for c in await client.search(name)
                if same_artist(c["label"], name)
                or (c["match"] and same_artist(c["match"], name))]
        entities = await client.get_entities([c["qid"] for c in hits[:5]])
        people = [c for c in hits[:5]
                  if wd.is_person(entities.get(c["qid"], {}))
                  and wd.is_musical_artist(entities.get(c["qid"], {}))]
        vouched = [c for c in people
                   if band_qid in wd.claim_item_ids(entities.get(c["qid"], {}), "P463")]
        chosen = vouched[0] if vouched else (people[0] if len(people) == 1 else None)
        if chosen is not None:
            qid = chosen["qid"]
            # somebody already answering to that identity IS them, under
            # whatever name the shelf knows them by — `luky` is the same person
            # as the infobox's `Dragan Lukić Lvky`, and a second row would be a
            # second him
            held = (await session.execute(
                select(Artist.id).where(Artist.wikidata_id == qid))).scalar_one_or_none()
            if held is not None:
                return held
    except Exception as exc:
        log.warning("wikidata lookup for band member %r failed: %s", name, exc)

    entity = entities.get(qid, {}) if qid else {}
    shadow = Artist(
        name=name,
        wikidata_id=qid,
        artist_type="person",
        image_url=wd.commons_image_url(entity) if entity else None,
        # who was in a band is not a shelf somebody asked to keep
        monitored=False,
        enrich_status=EnrichStatus.PENDING,
    )
    session.add(shadow)
    await session.flush()
    return shadow.id


async def _get_or_create_related(session, qid: str, entity: dict) -> int | None:
    """Find the related artist by QID or Deezer ID, or create a shadow row
    (monitored=False) so relations always point at real artist records."""
    result = await session.execute(select(Artist).where(Artist.wikidata_id == qid))
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing.id

    client = DeezerClient()
    try:
        deezer_id = await deezer.verified_claim_id(
            client, wd.first_claim(entity, "P2722"),
            wd.entity_display_name(entity) or "")
    finally:
        await client.close()
    if deezer_id is not None:
        result = await session.execute(select(Artist).where(Artist.deezer_id == deezer_id))
        existing = result.scalar_one_or_none()
        if existing is not None:
            if existing.wikidata_id is None:
                existing.wikidata_id = qid
            return existing.id

    name = wd.entity_display_name(entity)
    if not name:
        return None
    # no id match, but an existing library artist under the same (folded) name
    # OR one of the entity's own aliases IS this artist — attach the identity
    # instead of forking a duplicate shadow (a catalogue spells 'Vojko Vrućina', the
    # label is 'Vojko V', the alias bridges them). Only rows that carry no QID
    # yet are eligible, so a genuine homonym (already resolved to a different
    # QID) is never hijacked.
    known_names = [name] + [
        alias["value"]
        for lang in ("hr", "en")
        for alias in entity.get("aliases", {}).get(lang, [])
    ]
    for cand in (await session.execute(
        select(Artist).where(Artist.wikidata_id.is_(None)))).scalars():
        if any(same_artist(cand.name, n) for n in known_names):
            cand.wikidata_id = qid
            if cand.deezer_id is None and deezer_id is not None and (
                await session.execute(
                    select(Artist.id).where(Artist.deezer_id == deezer_id))
            ).scalar_one_or_none() is None:
                cand.deezer_id = deezer_id
            return cand.id
    shadow = Artist(
        name=name,
        deezer_id=deezer_id,
        wikidata_id=qid,
        artist_type="person" if wd.is_person(entity) else "group",
        image_url=wd.commons_image_url(entity),
        monitored=False,
        enrich_status=EnrichStatus.PENDING,
    )
    session.add(shadow)
    await session.flush()
    return shadow.id
