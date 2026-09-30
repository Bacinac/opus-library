"""Artist resource: catalog search (Deezer widened by Spotify), adding an act
to the library, the artist detail page the UI renders, and the manual levers
over identity — enrichment, Wikidata candidate pick, discography resync.
A unit because they all speak the same identity contract: an artist row is
adopted, never duplicated."""

import asyncio
import logging
import math
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from opus.music.pipeline import state
from opus.api.routers.music.shared import (
    EXTERNAL_LINK_TEMPLATES,
    _canonical,
    _mixed_sources,
    _quality_summary,
)
from opus.music.classify import classify_release, editions
from opus.db import get_session
from opus.music.metadata import discography, enrich, tracklists
from opus.music.metadata.deezer import DeezerClient
from opus.music.metadata.spotify import SpotifyClient, SpotifyCooldown, SpotifyError
from opus.music.metadata.wikidata import WikidataClient
from opus.models import (
    Artist,
    ArtistExternalId,
    ArtistRelation,
    Image,
    MusicDownload,
    MusicDownloadStatus,
    MusicFile,
    Release,
    ReleaseStatus,
    Track,
)
from opus.music.tagging import tagger
from opus.settings_store import current_runtime
from opus.api.routers.music.releases import _pick_edition, _track_playback
from opus.music.textnorm import PARENS, answers, norm, same_artist, title_score

router = APIRouter()
log = logging.getLogger("opus.api")


@router.get("/search/artists")
async def search_artists(q: str):
    config = await current_runtime()
    results = await _deezer_artist_results(q)
    # Spotify widens the net for acts Deezer does not carry (small local
    # bands); a hit whose name IS a Deezer hit is the same artist and stays
    # merged — enrichment links the ids later via Wikidata
    if config.get("spotify_client_id") and config.get("spotify_client_secret"):
        results += await _spotify_artist_results(q, config, results)
    results.sort(key=lambda r: _artist_result_rank(r, q), reverse=True)
    return results


async def _deezer_artist_results(q: str) -> list[dict]:
    client = DeezerClient()
    try:
        artists, tracks = await asyncio.gather(
            client.search_artists(q), client.search_tracks(q)
        )
        artists = [a for a in artists if answers(q, a["name"])][:10]
        direct_ids = {a["id"] for a in artists}
        matched_track = _artists_behind_tracks(q, tracks[:10])
        extra_ids = [i for i in matched_track if i not in direct_ids][:5]
        extras = await asyncio.gather(*(client.get_artist(i) for i in extra_ids))
        ordered = artists + list(extras)
        tops = await asyncio.gather(
            *(client.get_artist_top_tracks(a["id"]) for a in ordered)
        )
    finally:
        await client.close()
    return [
        {
            "deezer_id": a["id"],
            "spotify_id": None,
            "name": a["name"],
            "image_url": a.get("picture_medium"),
            "nb_album": a.get("nb_album"),
            "nb_fan": a.get("nb_fan"),
            "link": a.get("link"),
            "top_tracks": [t.get("title_short") or t.get("title") for t in top],
            "matched_track": None if a["id"] in direct_ids else matched_track.get(a["id"]),
        }
        for a, top in zip(ordered, tops)
    ]


def _artists_behind_tracks(q: str, tracks: list[dict]) -> dict[int, str]:
    """A query can name a song rather than an artist — the artists behind the
    best track hits, each with the track that matched."""
    matched: dict[int, str] = {}
    for track in tracks:
        track_artist = track.get("artist") or {}
        if track_artist.get("id") is None:
            continue
        title = track.get("title_short") or track["title"]
        if answers(q, title):
            matched.setdefault(track_artist["id"], title)
    return matched


async def _spotify_artist_results(q: str, config, found: list[dict]) -> list[dict]:
    spotify = SpotifyClient(config.get("spotify_client_id"),
                            config.get("spotify_client_secret"))
    try:
        hits = await spotify.search_artists(q)
        fresh = [h for h in hits
                 if answers(q, h["name"])
                 and not any(same_artist(h["name"], r["name"]) for r in found)][:5]
        # development-mode apps 403 the top-tracks endpoint — decoration
        # only, never the reason to drop the hits themselves
        spotify_tops = await asyncio.gather(
            *(spotify.artist_top_tracks(h["id"]) for h in fresh),
            return_exceptions=True,
        )
    except SpotifyCooldown as exc:
        log.info("spotify artist search skipped: %s", exc)
        return []
    except SpotifyError as exc:
        log.error("spotify artist search failed: %s", exc)
        return []
    finally:
        await spotify.close()
    return [
        {
            "deezer_id": None,
            "spotify_id": h["id"],
            "name": h["name"],
            "image_url": h.get("image_url"),
            "nb_album": None,
            "nb_fan": h.get("nb_fan"),
            "link": h.get("link"),
            "top_tracks": top if isinstance(top, list) else [],
            "matched_track": None,
        }
        for h, top in zip(fresh, spotify_tops)
    ]


def _artist_result_rank(r: dict, q: str) -> float:
    """Deezer's own order puts literal name matches first regardless of
    popularity; re-rank by query similarity with fan count as the prior (each
    10x in fans is worth 6 similarity points)."""
    sim = title_score(r["name"], q)
    if r["matched_track"]:
        sim = max(sim, 0.95 * title_score(r["matched_track"], q))
    return sim + 6 * math.log10((r["nb_fan"] or 0) + 1)


class AddArtistBody(BaseModel):
    deezer_id: int | None = None
    spotify_id: str | None = None
    monitored: bool = True


async def _add_spotify_artist(body: AddArtistBody, session: AsyncSession,
                              response: Response) -> dict:
    """A Spotify-only search hit (Deezer does not carry the act): adopt an
    existing row by external id or exact name before creating one — the
    identity contract from the import paths applies here too. Enrichment
    chains the discography, which reads the spotify external id."""
    config = await current_runtime()
    if not (config.get("spotify_client_id") and config.get("spotify_client_secret")):
        raise HTTPException(409, "spotify is not configured")
    spotify = SpotifyClient(config.get("spotify_client_id"),
                            config.get("spotify_client_secret"))
    try:
        data = await spotify.get_artist(body.spotify_id)
        try:
            albums = await spotify.artist_albums(body.spotify_id)
        except SpotifyError as exc:
            log.warning("spotify albums of %s could not be read: %s", body.spotify_id, exc)
            albums = []
    except SpotifyError as exc:
        raise HTTPException(502, f"spotify lookup failed: {exc}")
    finally:
        await spotify.close()

    created = False
    existing_ext = (await session.execute(
        select(ArtistExternalId).where(ArtistExternalId.source == "spotify",
                                       ArtistExternalId.external_id == str(body.spotify_id))
    )).scalar_one_or_none()
    if existing_ext is not None:
        artist = await session.get(Artist, existing_ext.artist_id)
        if artist.monitored:
            response.status_code = 200
            return {"id": artist.id, "already": True}
        artist.monitored = body.monitored
    else:
        artist = None
        for cand in (await session.execute(select(Artist))).scalars():
            if same_artist(cand.name, data["name"]):
                artist = cand
                if artist.monitored and any(
                    e.source == "spotify" for e in await artist.awaitable_attrs.external_ids
                ):
                    response.status_code = 200
                    return {"id": artist.id, "already": True}
                artist.monitored = body.monitored
                break
        if artist is None:
            artist = Artist(name=data["name"], image_url=data.get("image_url"),
                            monitored=body.monitored)
            session.add(artist)
            await session.flush()
            created = True
        await session.execute(
            pg_insert(ArtistExternalId)
            .values(artist_id=artist.id, source="spotify",
                    external_id=str(body.spotify_id))
            .on_conflict_do_nothing()
        )
    if artist.image_url is None:
        artist.image_url = data.get("image_url")
    await session.commit()
    await _must_have_a_year(session, artist, adopted=not created, records=albums)
    if artist.enrich_status != "resolved":
        enrich.spawn_enrich(artist.id)
    else:
        discography.spawn_sync(artist.id)
    return {"id": artist.id, "name": artist.name, "releases": 0}


async def _must_have_a_year(session, artist, adopted: bool,
                            records: list[dict] | None = None) -> None:
    """An artist arrives with the year they began or does not arrive.

    Without it they cannot be put in order among the others, and a shelf that
    cannot be ordered is a list. So the enrichment that finds it runs HERE, in
    front of whoever is adding them, rather than behind their back afterwards —
    and if it comes back empty the row goes with it rather than sitting on the
    shelf as a gap somebody has to notice later.

    Wikidata is asked first, Wikipedia after it, and failing both the year of
    the earliest record being added — an act nobody wrote an article about
    still has a first record. What is left after all three is an artist with no
    records either, and that one is turned away.
    """
    if artist.begin_year is not None:
        return
    await session.commit()
    await enrich.enrich_artist(artist.id, chain_discography=False)
    await session.refresh(artist)
    if artist.begin_year is None:
        # the records being added are not in the table yet, so the earliest of
        # them is offered here rather than looked up
        years = [y for y in (enrich.year_of_record(r.get("release_date"))
                             for r in records or ()) if y is not None]
        if years:
            artist.begin_year = min(years)
    if artist.begin_year is not None:
        return
    name = artist.name
    if adopted:
        artist.monitored = False
    else:
        await session.delete(artist)
    await session.commit()
    raise HTTPException(
        422,
        f"{name}: no year of origin could be found, so nothing was added. "
        "An artist without one cannot be put in order with the others.",
    )


@router.post("/artists", status_code=201)
async def add_artist(body: AddArtistBody, response: Response,
                     session: AsyncSession = Depends(get_session)):
    if body.deezer_id is None:
        if body.spotify_id is None:
            raise HTTPException(400, "deezer_id or spotify_id required")
        return await _add_spotify_artist(body, session, response)
    result = await session.execute(select(Artist).where(Artist.deezer_id == body.deezer_id))
    existing = result.scalar_one_or_none()
    if existing is not None and existing.monitored:
        response.status_code = 200
        return {"id": existing.id, "already": True}

    client = DeezerClient()
    try:
        artist_data = await client.get_artist(body.deezer_id)
        albums = await client.get_artist_albums(body.deezer_id)
    finally:
        await client.close()

    adopted = existing is not None
    if adopted:
        # shadow row created from a relation — adopt it into the library
        artist = existing
        artist.monitored = body.monitored
        if artist.image_url is None:
            artist.image_url = artist_data.get("picture_medium")
    else:
        artist = Artist(
            name=artist_data["name"],
            deezer_id=artist_data["id"],
            image_url=artist_data.get("picture_medium"),
            monitored=body.monitored,
        )
        session.add(artist)
        await session.flush()

    await _must_have_a_year(session, artist, adopted, albums)

    # global check: Deezer lists shared compilations under every contributing
    # artist, and deezer_id is unique across the table
    have = {
        r.deezer_id
        for r in (await session.execute(
            select(Release.deezer_id).where(
                Release.deezer_id.in_([a["id"] for a in albums])
            )
        )).all()
    }
    added = 0
    for album in albums:
        if album["id"] in have:
            continue
        session.add(Release(
            artist_id=artist.id,
            title=album["title"],
            deezer_id=album["id"],
            release_date=album.get("release_date"),
            record_type=album.get("record_type"),
            cover_url=album.get("cover_medium"),
        ))
        added += 1
    await session.commit()

    if artist.enrich_status != "resolved":
        enrich.spawn_enrich(artist.id)
    return {"id": artist.id, "name": artist.name, "releases": added}


async def _widest(session) -> dict[int, str]:
    """The widest landscape picture held of each artist.

    A portrait is the wrong shape to lie behind a line of text, and the
    candidates gathered for the artwork picker already include the band photos
    Discogs and Wikimedia carry, which are the right one."""
    rows = (await session.execute(
        select(Image.entity_id, Image.url, Image.width, Image.height)
        .where(Image.entity_type == "artist",
               Image.width.is_not(None), Image.height.is_not(None))
        .order_by(Image.width.desc())
    )).all()
    widest: dict[int, str] = {}
    for artist_id, url, width, height in rows:
        if artist_id in widest or height <= 0 or width / height < 1.4:
            continue
        widest[artist_id] = url
    return widest


def _blurb(text: str | None, limit: int = 300) -> str:
    """As much of the bio as a shelf can show without carrying the whole of it.
    The band under a shelf reads two lines; the rest belongs to the artist's own
    screen, which asks for them one at a time."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit].rsplit(" ", 1)[0] + "…"


@router.get("/artists")
async def list_artists(ids: str | None = None, session: AsyncSession = Depends(get_session)):
    """The whole shelf, or with `ids` only the artists named: whoever needs ten
    of them for a row or a search answer does not carry the other three hundred."""
    shelf = select(Artist).where(Artist.monitored)
    if ids is not None:
        shelf = shelf.where(Artist.id.in_(
            [int(i) for i in ids.split(",") if i.strip().isdigit()]))
    # By name, though films and series come back newest first: every artist here
    # carries the same arrival date, because they came in one migration, so
    # "newest" resolves to the tiebreak and the shelf opens on Š, Š, Š, Đ, Đ.
    result = await session.execute(shelf.order_by(Artist.name))
    counts = dict((await session.execute(
        select(Release.artist_id, func.count(Release.id)).group_by(Release.artist_id)
    )).all())
    # and of those, the ones there is something to play. `releases` is what the
    # catalogue knows OF an artist, held or not, and a screen that adds it up
    # says twenty thousand albums of a shelf holding three and a half thousand.
    held = dict((await session.execute(
        select(Release.artist_id, func.count(func.distinct(Release.id)))
        .join(Track, Track.release_id == Release.id)
        .join(MusicFile, MusicFile.track_id == Track.id)
        .group_by(Release.artist_id)
    )).all())
    wide = await _widest(session)
    return [
        # begin_year travels with the listing because it is what an artist IS —
        # the year they turned up — and a shelf that offers to be sorted by it
        # should not have to open every artist to find out. The rest is what the
        # band under the shelf says about whoever has focus
        {"id": a.id, "name": a.name, "deezer_id": a.deezer_id,
         # when this house got them, so a shelf can say "new" and mean it
         "added_at": a.created_at.isoformat() if a.created_at else None,
         "image_url": a.image_url, "begin_year": a.begin_year,
         "end_year": a.end_year, "country": a.country, "country_hr": a.country_hr,
         "releases": counts.get(a.id, 0), "held": held.get(a.id, 0),
         "blurb": _blurb(a.bio),
         "backdrop_url": wide.get(a.id)}
        for a in result.scalars()
    ]


# which derived categories the release_filter setting hides outright;
# compilations/live are never hidden — the UI demotes them to a side section
HIDDEN_CATEGORIES = {
    "albums": {"ep", "single"},
    "albums_eps": {"single"},
    "all": set(),
}


@router.get("/artists/{artist_id}")
async def get_artist(artist_id: int, session: AsyncSession = Depends(get_session)):
    artist = await _artist_with_catalogue(session, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    config = await current_runtime()
    hidden = HIDDEN_CATEGORIES.get(config.get("release_filter"), set())
    authority = _vouched_for(artist)
    releases = _shown_releases(artist.releases, hidden)
    pressings = _pressings(releases)
    members, groups = await _line_up(session, artist_id)
    unmatched_by_release = await _unmatched_by_release(session, releases)
    progress_by_release = await _progress_by_release(session, releases)

    return {
        "id": artist.id,
        "name": artist.name,
        "image_url": artist.image_url,
        "backdrop_url": (await _widest(session)).get(artist.id),
        "monitored": artist.monitored,
        "deezer_id": artist.deezer_id,
        "wikidata_id": artist.wikidata_id,
        "artist_type": artist.artist_type,
        "country": artist.country,
        "country_hr": artist.country_hr,
        "begin_year": artist.begin_year,
        "end_year": artist.end_year,
        "bio": artist.bio,
        "enrich_status": artist.enrich_status,
        "links": _artist_links(artist),
        "members": members,
        "groups": groups,
        "releases": [
            _release_view(artist, r, category, authority, pressings[r.id],
                          unmatched_by_release.get(r.id, 0), progress_by_release.get(r.id))
            for r, category in releases
        ],
    }


@router.get("/artists/{artist_id}/tracks")
async def artist_tracks(artist_id: int, prefer: str = "stereo",
                        session: AsyncSession = Depends(get_session)):
    """Every song of an artist's held once, oldest record first, as something
    to put on. A song that turns up again on a compilation or a live record is
    the song a listener already has; it plays from the record it first came
    out on. Which of them comes first is the player's to decide — it knows who
    has been listening to what."""
    artist = await _artist_with_catalogue(session, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    releases = _shown_releases(artist.releases, set())
    pressings = _pressings(releases)
    held: dict[str, dict] = {}
    for release, _ in sorted(releases, key=lambda pair: pair[0].release_date or "9999"):
        if not pressings[release.id][0]:
            continue
        for track in sorted(release.tracks, key=lambda t: t.position):
            media = _pick_edition(track, prefer)
            song = " ".join(norm(PARENS.sub("", track.title)).split())
            if media is not None and song not in held:
                held[song] = _track_playback(track, media, release, artist.name)
    return list(held.values())


async def _artist_with_catalogue(session, artist_id: int) -> Artist | None:
    return (await session.execute(
        select(Artist)
        .where(Artist.id == artist_id)
        .options(
            selectinload(Artist.releases)
            .selectinload(Release.tracks)
            .selectinload(Track.files),
            selectinload(Artist.releases).selectinload(Release.variants),
            selectinload(Artist.external_ids),
        )
    )).scalar_one_or_none()


def _vouched_for(artist: Artist) -> bool:
    # whether anything vouches for this artist at all; where nothing does, the
    # classification has to stand in for it
    return bool(artist.wiki_studio_albums) or any(
        r.wikidata_id is not None for r in artist.releases)


def _shown_releases(releases: list[Release], hidden: set[str]) -> list[tuple[Release, str]]:
    shown = []
    for r in releases:
        category = classify_release(r.title, r.record_type)
        in_library = r.status != ReleaseStatus.NONE or any(t.files for t in r.tracks)
        # a hidden category stays visible once we actually hold or track it
        if category in hidden and not in_library:
            continue
        shown.append((r, category))
    shown.sort(key=lambda pair: pair[0].release_date or "", reverse=True)
    return shown


def _pressings(releases: list[tuple[Release, str]]) -> dict[int, tuple[bool, str, str | None]]:
    # said here rather than by whoever draws the list: the player asks the same
    # question of the same records, and a rule kept in one client is a rule the
    # other one does not have
    return editions([
        (r.id, r.title, r.release_date, sum(1 for t in r.tracks if t.files))
        for r, _ in releases])


async def _line_up(session, artist_id: int) -> tuple[list[dict], list[dict]]:
    members = (await session.execute(
        select(Artist)
        .join(ArtistRelation, ArtistRelation.artist_id == Artist.id)
        .where(ArtistRelation.related_artist_id == artist_id,
               ArtistRelation.relation == "member_of")
        .order_by(Artist.name)
    )).scalars().all()
    groups = (await session.execute(
        select(Artist)
        .join(ArtistRelation, ArtistRelation.related_artist_id == Artist.id)
        .where(ArtistRelation.artist_id == artist_id,
               ArtistRelation.relation == "member_of")
        .order_by(Artist.name)
    )).scalars().all()
    return _related(members), _related(groups)


def _related(rows) -> list[dict]:
    return [{"id": a.id, "name": a.name, "monitored": a.monitored} for a in rows]


def _artist_links(artist: Artist) -> list[dict]:
    links = []
    if artist.deezer_id:
        links.append({"source": "deezer", "url": f"https://www.deezer.com/artist/{artist.deezer_id}"})
    for ext in artist.external_ids:
        template = EXTERNAL_LINK_TEMPLATES.get(ext.source)
        if template:
            links.append({"source": ext.source, "url": template.format(ext.external_id)})
    if artist.wikidata_id:
        links.append({"source": "wikidata", "url": f"https://www.wikidata.org/wiki/{artist.wikidata_id}"})
    if artist.bio_url:
        links.append({"source": "wikipedia", "url": artist.bio_url})
    return links


async def _unmatched_by_release(session, releases: list[tuple[Release, str]]) -> dict[int, int]:
    # folder-mates that matched no track — a release with leftovers is not
    # fully green whatever its edition claims
    unmatched_by_release: dict[int, int] = {}
    folders: dict[str, list[int]] = {}
    for r, _ in releases:
        first = next((f.path for t in r.tracks for f in t.files), None)
        if first:
            folders.setdefault(str(PurePosixPath(first).parent), []).append(r.id)
    for folder, release_ids in folders.items():
        count = (await session.execute(
            select(func.count()).select_from(MusicFile)
            .where(MusicFile.track_id.is_(None),
                   MusicFile.path.startswith(f"{folder}/", autoescape=True))
        )).scalar()
        for rid in release_ids:
            unmatched_by_release[rid] = count
    return unmatched_by_release


async def _progress_by_release(session,
                               releases: list[tuple[Release, str]]) -> dict[int, float | None]:
    active_downloads = (await session.execute(
        select(MusicDownload.release_id, MusicDownload.id)
        .where(
            MusicDownload.release_id.in_([r.id for r, _ in releases]),
            MusicDownload.status.in_((MusicDownloadStatus.QUEUED, MusicDownloadStatus.DOWNLOADING,
                                 MusicDownloadStatus.IMPORTING)),
        )
        .order_by(MusicDownload.id)
    )).all()
    return {
        release_id: (state.live.get(download_id) or {}).get("progress")
        for release_id, download_id in active_downloads
    }


def _release_view(artist: Artist, r: Release, category: str, authority: bool,
                  pressing: tuple[bool, str, str | None], unmatched_files: int,
                  progress: float | None) -> dict:
    return {
        "id": r.id,
        "title": r.title,
        "release_date": r.release_date,
        "category": category,
        "cover_url": r.cover_url,
        "status": r.status,
        "searching_channel": state.searching.get(r.id),
        "progress": progress,
        "quality": _quality_summary(r.tracks),
        "files_linked": sum(1 for t in r.tracks if t.files),
        # which records of this music are on the shelf: "5.1" beside
        # nothing means the surround mix is here and the stereo one is
        # not, which is a thing worth seeing at a glance
        "editions": sorted(
            {tagger.edition_of(f.channels, f.codec) or "stereo"
             for t in r.tracks for f in t.files}
        ),
        "unmatched_files": unmatched_files,
        "mixed_sources": _mixed_sources(r),
        "variants": [
            {"variant": v.variant, "source": v.source,
             "external_id": v.external_id,
             "seen_at": v.seen_at.isoformat()}
            for v in sorted(r.variants, key=lambda v: (v.variant, v.source))
        ],
        "canonical": _canonical(artist, r, category, authority),
        # which pressing stands for the record, and what the record
        # itself is called and dated — a remaster is not a second line
        "stands": pressing[0],
        "record_title": pressing[1],
        "record_date": pressing[2],
        "track_count": r.track_count or (len(r.tracks) or None),
        "sources": [
            source for source, present in (
                ("deezer", r.deezer_id), ("discogs", r.discogs_id),
                ("spotify", r.spotify_id), ("wikidata", r.wikidata_id),
            ) if present
        ],
    }


@router.post("/artists/{artist_id}/discography/sync")
async def sync_discography(artist_id: int, session: AsyncSession = Depends(get_session)):
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    try:
        return await discography.sync_artist(artist_id)
    except tracklists.DiscographyError as exc:
        raise HTTPException(400, str(exc))


@router.post("/artists/{artist_id}/enrich", status_code=202)
async def enrich_artist(artist_id: int, session: AsyncSession = Depends(get_session)):
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    enrich.spawn_enrich(artist_id)
    return {"status": "queued"}


@router.get("/artists/{artist_id}/identity-candidates")
async def identity_candidates(artist_id: int, q: str, session: AsyncSession = Depends(get_session)):
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    client = WikidataClient()
    try:
        return await client.search(q)
    finally:
        await client.close()


class IdentityBody(BaseModel):
    qid: str


@router.put("/artists/{artist_id}/identity", status_code=202)
async def set_identity(body: IdentityBody, artist_id: int,
                       session: AsyncSession = Depends(get_session)):
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404, "artist not found")
    enrich.spawn_enrich(artist_id, body.qid)
    return {"status": "queued"}
