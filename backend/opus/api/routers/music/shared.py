"""Helpers more than one router needs: the release/artist view fragments
(external links, quality summary, canonical-album test), the destructive
filesystem primitives (purge a release's files, prune the folders they leave
behind) and the tag-write error translation. A unit because each of these is
shared by two or more resource routers and must behave identically in all of
them."""

import asyncio
import errno
from pathlib import Path

from fastapi import HTTPException

from opus.models import Artist, Release
from opus.music.tagging import tagger
from opus.music.textnorm import album_score

EXTERNAL_LINK_TEMPLATES = {
    "spotify": "https://open.spotify.com/artist/{}",
    "discogs": "https://www.discogs.com/artist/{}",
    "musicbrainz": "https://musicbrainz.org/artist/{}",
}

_COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png")


def edition_of(media) -> str:
    """Which record of the music this file is. Stereo is the album as everyone
    has it; wider is another mix of it; DSD is the same recording again, in the
    one format the DAC plays natively — each an edition filed beside the
    others, never over them."""
    if (media.channels or 0) > 2:
        return "surround"
    if (media.codec or "").lower() in tagger.DSD_CODECS:
        return "dsd"
    return "stereo"


def _by_edition(infos: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for f in infos:
        grouped.setdefault(edition_of(f), []).append(f)
    return grouped


def _quality_summary(tracks) -> dict | None:
    """The album's quality is its WEAKEST track, never its best: a badge built
    from maxima made an album of two 24/96 tracks and nine 16/44 ones read as
    pristine 24/96. The channel layout has no weakest — 5.1 is not a better
    stereo but a different mix — so it is stated only where every file agrees,
    and where they do not, the disagreement itself is what the album has.

    An album may be held in more than one edition, and a difference BETWEEN the
    editions is not a disagreement — it is the point. So the reading is of one
    edition at a time: the stereo master where there is one, since that is the
    record an album is known by, and otherwise the one edition there is."""
    infos = [f for t in tracks for f in t.files if f.codec]
    if not infos:
        return None
    editions = _by_edition(infos)
    read = editions.get("stereo") or next(iter(editions.values()))

    def disagrees(group: list) -> bool:
        return (len({f.codec for f in group}) > 1
                or len({f.sample_rate_hz for f in group if f.sample_rate_hz}) > 1
                or len({f.bit_depth for f in group if f.bit_depth}) > 1
                or len({f.channels for f in group if f.channels}) > 1)

    codecs = {f.codec for f in read}
    rates = {f.sample_rate_hz for f in read if f.sample_rate_hz}
    depths = {f.bit_depth for f in read if f.bit_depth}
    channels = {f.channels for f in read if f.channels}
    return {
        "codec": read[0].codec if len(codecs) == 1 else "mixed",
        "bitrate_kbps": min((f.bitrate_kbps for f in read if f.bitrate_kbps), default=None),
        "sample_rate_hz": min(rates, default=None),
        "bit_depth": min(depths, default=None),
        "channels": next(iter(channels)) if len(channels) == 1 else None,
        "mixed": any(disagrees(group) for group in editions.values()),
    }


def _mixed_sources(r: Release) -> bool:
    """Files of ONE EDITION of an album that came from more than one download —
    two rips stitched together. Two editions come from two downloads by their
    nature, and that is not stitching. Files adopted from the existing library
    carry no download and are not evidence either way."""
    files = [f for t in r.tracks for f in t.files if f.download_id is not None]
    return any(len({f.download_id for f in group}) > 1
               for group in _by_edition(files).values())


def _canonical(artist: Artist, r: Release, category: str,
               authority: bool = True) -> bool:
    """Whether this is one of the records the artist's discography is made of.

    The Wikipedia studio-albums list decides WHICH records those are; it cannot
    decide that a single is one. Matching on the title alone let `The Ghost Of
    Tom Joad - EP` and `Both Sides, Now (2021 Remaster)` — an EP and a single,
    and both say so at the source — stand in for the albums they are named
    after, and be counted as albums missing from the shelf.

    `authority` says whether anything at all vouches for this artist: a
    Wikipedia list, or a Wikidata identity on any of their records. Where
    nothing does — twenty-three of them here, `Bare & Plaćenici` among them —
    demanding a Wikidata id makes every record they ever made uncanonical, and
    a page that counts two albums shows neither. There the classification is all
    there is, so it is what decides."""
    if category != "studio":
        return False
    # the artist's Wikipedia studio-albums list is the main-table
    # authority; without one, Wikidata-backed studio typing decides
    if artist.wiki_studio_albums:
        return any(album_score(r.title, s) >= 85
                   for s in artist.wiki_studio_albums)
    if not authority:
        return True
    return r.wikidata_id is not None


def _artwork_entity(entity: str) -> str:
    if entity not in ("artists", "releases"):
        raise HTTPException(404, "unknown entity")
    return "artist" if entity == "artists" else "release"


async def _write_tag(fn, *args) -> None:
    """Run a mutagen tag write off the event loop and translate its failures
    into the answers the UI knows how to show."""
    try:
        await asyncio.to_thread(fn, *args)
    except tagger.ImportError_ as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        # mutagen wraps the underlying OSError in MutagenError
        cause = exc if isinstance(exc, OSError) else exc.__cause__
        if isinstance(cause, OSError) and cause.errno == errno.EROFS:
            raise HTTPException(
                409, "music library is mounted read-only (dev); tag fixing works on production"
            )
        raise HTTPException(500, f"tag write failed: {exc}")


async def _prune_empty_dirs(start: Path, music_root: Path) -> None:
    cursor = start
    while cursor != music_root and music_root in cursor.resolve().parents:
        try:
            if any(cursor.iterdir()):
                break
            await asyncio.to_thread(cursor.rmdir)
        except OSError:
            break
        cursor = cursor.parent


async def _purge_release_files(session, release: Release, music_root: Path) -> int:
    """Delete the release's audio files from disk and their rows; drop the
    album's cover art and any now-empty folders (deepest first). The caller
    sets the follow-up status and commits. Never touches anything outside
    the library root."""
    files = [f for t in release.tracks for f in t.files]
    folders: set[Path] = set()
    removed = 0
    for f in files:
        path = Path(f.path)
        if music_root not in path.resolve().parents:
            continue  # never delete outside the library
        try:
            if path.exists():
                await asyncio.to_thread(path.unlink)
            removed += 1
        except OSError as exc:
            raise HTTPException(500, f"failed to delete {path.name}: {exc}")
        folders.add(path.parent)
        await session.delete(f)

    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        for cover in _COVER_NAMES:
            art = folder / cover
            if art.exists():
                await asyncio.to_thread(art.unlink)
        await _prune_empty_dirs(folder, music_root)
    return removed
