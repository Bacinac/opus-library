"""Edition evidence: proof that a folder holding every file it has is already
a COMPLETE edition, even though the catalog row lists more tracks. Four
independent sources can supply that proof and each one costs throttled API
calls, so they run cheapest-first and stop at the first that agrees with the
files on disk. Deciding only — what a confirmed edition does to the rows is
the caller's business."""

import logging
from dataclasses import dataclass

from opus.music.library import albumsearch, matching
from opus.music.library.artists import ALBUM_MATCH_THRESHOLD
from opus.music.metadata import variants
from opus.music.metadata.discogs import DiscogsClient, DiscogsError
from opus.music.metadata.musicbrainz import MusicBrainzError
from opus.models import MusicFile, Release
from opus.music.textnorm import PARENS, album_score, same_name

log = logging.getLogger("opus.libimport")


@dataclass(frozen=True)
class Edition:
    """The proof, and the tracklist it was proved against. `titles` is None for
    the count-only rung: enough to accept that the files ARE a full edition,
    not enough to rewrite a tracklist from."""

    evidence: str
    titles: list[str] | None = None


async def confirm_edition(session, discogs: DiscogsClient | None,
                          best_release: Release, releases: list[Release],
                          tag_artist: str, tag_album: str, matched: int,
                          rows: list[MusicFile]) -> Edition | None:
    """The edition the files actually are, or None when nothing proves one.
    Evidence, cheapest first: the Discogs master's original count, the
    Wikipedia tracklist, any MusicBrainz edition, any Discogs version."""
    master_id = (await _master_of(discogs, best_release, releases, tag_artist, tag_album)
                 if discogs is not None else None)
    return (await _by_master_count(discogs, master_id, matched)
            or await _by_wikipedia(session, best_release, matched, rows)
            or await _by_musicbrainz(tag_artist, tag_album, matched, rows)
            or await _by_discogs_version(session, discogs, master_id, best_release,
                                         matched, rows))


async def _master_of(discogs: DiscogsClient, release: Release, releases: list[Release],
                     tag_artist: str, tag_album: str) -> int | None:
    if release.discogs_id is not None:
        return release.discogs_id
    # strips "(Deluxe Edition)"-style qualifiers when hunting a sibling row's
    # master
    sibling = next((r.discogs_id for r in releases
                    if r.discogs_id is not None
                    and same_name(PARENS.sub(" ", r.title), tag_album)), None)
    if sibling is not None:
        return sibling
    try:
        masters = (await discogs.search_masters(tag_artist, tag_album))[:3]
    except DiscogsError:
        return None
    return next((m["id"] for m in masters
                 if album_score(m["title"], tag_album) >= ALBUM_MATCH_THRESHOLD), None)


async def _by_master_count(discogs: DiscogsClient | None, master_id: int | None,
                           matched: int) -> Edition | None:
    if discogs is None or master_id is None:
        return None
    try:
        original = (await discogs.master_details(master_id)).get("track_count") or 0
    except DiscogsError:
        return None
    return Edition("discogs master") if original and matched >= original else None


async def _by_wikipedia(session, release: Release, matched: int,
                        rows: list[MusicFile]) -> Edition | None:
    if not release.wikidata_id:
        return None
    try:
        titles = await albumsearch.wikipedia_titles(session, release.wikidata_id)
    except Exception as exc:
        log.error("wikipedia edition check failed for %s: %s", release.wikidata_id, exc)
        return None
    if titles and len(titles) == matched and matching.eval_titles(titles, rows) == matched:
        return Edition("wikipedia", titles)
    return None


async def _by_musicbrainz(tag_artist: str, tag_album: str, matched: int,
                          rows: list[MusicFile]) -> Edition | None:
    try:
        found = await albumsearch.mb.release_editions(tag_artist, tag_album)
        for edition in [e for e in found if e["track_count"] == matched][:3]:
            titles = await albumsearch.mb.release_titles(edition["id"])
            if matching.eval_titles(titles, rows) == matched:
                return Edition("musicbrainz", titles)
    except MusicBrainzError as exc:
        log.error("musicbrainz edition check failed for %s — %s: %s",
                  tag_artist, tag_album, exc)
    return None


async def _by_discogs_version(session, discogs: DiscogsClient | None, master_id: int | None,
                              release: Release, matched: int,
                              rows: list[MusicFile]) -> Edition | None:
    if discogs is None or master_id is None:
        return None
    try:
        all_versions = await discogs.master_versions(master_id)
    except DiscogsError:
        return None
    # the full listing is already in hand — the mixes among the pressings
    # cost nothing to notice on the way past
    for sighting in variants.from_discogs_versions(all_versions, release.title):
        await variants.note(session, release.id, sighting)
    for version in albumsearch.version_candidates(all_versions):
        try:
            titles = [t["title"] for t in await discogs.release_tracklist(version["id"])]
        except DiscogsError:
            continue
        if len(titles) == matched and matching.eval_titles(titles, rows) == matched:
            return Edition("discogs version", titles)
    return None
