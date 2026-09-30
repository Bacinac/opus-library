"""Per-folder match decision: which release, if any, this folder IS. The fast
gate settles a folder that is already green from the DB alone; Tier 1 ranks and
scores the artist's existing editions; Tier 2 escalates into the full
multi-source machinery only when Tier 1 is not a perfect fit. Everything here
judges ONE folder against ONE already-resolved artist — the walk, the phases
and the report belong to the scan."""

import logging
from pathlib import Path

from sqlalchemy import func, select

from opus.db import SessionLocal
from opus.music.library import albumsearch, editions, matching, scan
from opus.music.library.artists import ALBUM_MATCH_THRESHOLD
from opus.music.metadata.catalog import CatalogClient
from opus.music.metadata.discogs import DiscogsClient
from opus.music.metadata.spotify import SpotifyClient
from opus.music.metadata.tracklists import DiscographyError, ensure_tracks
from opus.models import (
    Artist,
    MusicFile,
    Release,
    ReleaseStatus,
    Track,
)
from opus.music.textnorm import album_score, same_name

log = logging.getLogger("opus.libimport")


def _rank_candidates(releases: list[Release], tag_album: str,
                     file_count: int) -> list[tuple[float, Release]]:
    """The releases whose title matches the folder's album, ranked — shared by
    the fast gate and the full matcher."""
    candidates = [
        (score, release)
        for release in releases
        if (score := album_score(release.title, tag_album)) >= ALBUM_MATCH_THRESHOLD
    ]
    return sorted(
        candidates, key=lambda p: matching.rank_release(p, tag_album, file_count),
        reverse=True,
    )


async def _artist_releases(session, artist: Artist) -> list[Release]:
    return (await session.execute(
        select(Release).where(Release.artist_id == artist.id)
    )).scalars().all()


async def _fast_gate(session, artist: Artist, tag_album: str,
                     files: list[Path], paths: list[str],
                     directory: Path, root: Path) -> bool:
    """True when the folder is already green in the DB: a COMPLETE release that
    still links every file and has no unfilled track. Cheap, no network — the
    top-ranked row is often the bare title while the adopted one carries a
    '(Special Edition)'-style qualifier, so ANY complete candidate counts."""
    releases = await _artist_releases(session, artist)
    for _, release in _rank_candidates(releases, tag_album, len(files)):
        if release.status != ReleaseStatus.COMPLETE:
            continue
        linked = (await session.execute(
            select(func.count()).select_from(MusicFile)
            .join(Track, MusicFile.track_id == Track.id)
            .where(Track.release_id == release.id, MusicFile.path.in_(paths))
        )).scalar()
        unfilled = (await session.execute(
            select(func.count()).select_from(Track)
            .outerjoin(MusicFile, MusicFile.track_id == Track.id)
            .where(Track.release_id == release.id, MusicFile.id.is_(None))
        )).scalar()
        if linked == len(files) and unfilled == 0:
            count = release.track_count or 0
            scan.record(directory, root, "adopted", artist=artist.name,
                        artist_id=artist.id, album=release.title,
                        matched=count, total=count, reason="already_complete")
            scan.job.state["matched"] += 1
            return True
    return False


async def fast_gate_folder(artist_id: int, tag_album: str, files: list[Path],
                           directory: Path, root: Path) -> bool:
    """Own-session wrapper around _fast_gate for the Phase 3 match worker."""
    paths = [str(p) for p in files]
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        return await _fast_gate(session, artist, tag_album, files, paths,
                                directory, root)


async def match_folder(catalog: CatalogClient, discogs: DiscogsClient | None,
                       spotify: SpotifyClient | None, artist_id: int,
                       tag_artist: str, tag_album: str,
                       directory: Path, files: list[Path], root: Path,
                       progress: dict):
    """Phase 3: match a folder against an ALREADY-resolved artist. Tier 1 scores
    the artist's existing editions; Tier 2 escalates into the full multi-source
    machinery only when Tier 1 is not a perfect fit. The artist and the file
    rows already exist (phases 1 and 2) — no inventory or creation here.
    `progress["escalated"]` is set so the caller can count the deep pass even
    when this raises."""
    paths = [str(p) for p in files]
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        releases = await _artist_releases(session, artist)
        rows = (await session.execute(
            select(MusicFile).where(MusicFile.path.in_(paths))
        )).scalars().all()
        evaluated = await _evaluate_editions(
            session, _rank_candidates(releases, tag_album, len(files)), rows,
            tag_album, directory, len(files))

        best_now = max(
            evaluated,
            key=lambda e: (len(e[2]), len(e[2]) == len(e[1])),
        ) if evaluated else None
        if best_now is None or not _fits(*best_now, len(files)):
            # not fully green yet — this folder escalates into the full
            # multi-source machinery; only these count toward the deep pass
            progress["escalated"] = True
            scan.job.state["deep_total"] += 1
            scan.job.state["deep_current"] = str(directory.relative_to(root))
            found = await _escalate(
                session, catalog, discogs, spotify, artist, tag_artist, tag_album,
                rows, len(files), len(best_now[2]) if best_now else 0)
            if found is not None:
                evaluated.append(found)
            if not evaluated:
                await session.commit()  # keep the artist and any fallback release
                scan.record(directory, root, "album_not_found",
                            artist=artist.name, artist_id=artist.id,
                            album=tag_album, reason="no_catalog_candidate")
                return

        best_release, tracks, pairs = _chosen_edition(evaluated, len(files))
        _relink(rows, pairs)
        matched = len(pairs)

        complete = matched == len(tracks)
        # a folder holding EVERY file of a real edition is complete even when
        # the catalog row carries extra (bonus/streaming) tracks — the edition
        # HE holds is the reference.
        edition_confirmed = (not complete and matched == len(files)
                             and await _confirm_edition(
                                 session, discogs, best_release, releases,
                                 tag_artist, tag_album, rows, tracks, pairs))
        complete = complete or edition_confirmed
        total = matched if edition_confirmed else len(tracks)

        _settle_status(best_release, complete)
        # commit BEFORE recording, so a failed commit doesn't leave a success
        # entry the worker then double-records as an error
        await session.commit()
        if complete:
            scan.job.state["matched"] += 1
        scan.record(directory, root, "adopted" if complete else "partial",
                    artist=artist.name, artist_id=artist.id, album=best_release.title,
                    matched=matched, total=total, reason=_reason(complete, edition_confirmed))
        scan.job.state["adopted_tracks"] += matched


def _chosen_edition(evaluated: list[tuple[Release, list, dict]],
                    file_count: int) -> tuple[Release, list, dict]:
    return max(
        evaluated,
        key=lambda e: (
            len(e[2]),                          # most files linked first
            len(e[2]) == len(e[1]),             # then the complete edition
            -abs(len(e[1]) - file_count),       # then closest track count
        ),
    )


def _relink(rows: list, pairs: dict):
    row_by_id = {row.id: row for row in rows}
    for row in rows:
        row.track_id = None  # fresh rematch of this folder
    for track_id, row_id in pairs.items():
        row_by_id[row_id].track_id = track_id


def _settle_status(release: Release, complete: bool):
    """Green means the chosen edition is complete — every catalog track is
    present. Extra files beyond it (alternate takes, an FTD collector's box no
    streaming catalog lists as separate tracks) are bonus, not missing: the
    escalation already hunted a fuller edition and found none, so re-flagging
    the folder partial forever just re-hunts a catalog gap that never fills.
    They stay visible under "unmatched"."""
    if complete:
        release.status = ReleaseStatus.COMPLETE
    elif release.status == ReleaseStatus.COMPLETE:
        release.status = ReleaseStatus.NONE


def _fits(release: Release, tracks: list, pairs: dict, file_count: int) -> bool:
    return len(pairs) == len(tracks) == file_count


def _reason(complete: bool, edition_confirmed: bool) -> str:
    if edition_confirmed:
        return "edition_confirmed"
    return "all_tracks_matched" if complete else "partial_track_match"


async def _evaluate_editions(session, ordered: list[tuple[float, Release]], rows: list,
                             tag_album: str, directory: Path,
                             file_count: int) -> list[tuple[Release, list, dict]]:
    """Up to 4 candidate EDITIONS, each with what it would link — the one that
    matches the folder completely has to be among them: a partial against the
    Deluxe must lose to a full match against the standard edition."""
    evaluated: list[tuple[Release, list, dict]] = []
    for _, release in ordered[:4]:
        try:
            tracks = await ensure_tracks(session, release)
        except DiscographyError:
            continue
        # a 150-track box set must not swallow a 10-file album folder —
        # unless the folder IS that release by name, where an incomplete
        # rip of a 27-track live set belongs to the set it came from
        # ('Steel Wheels Live'), not to some smaller edition dug up later
        if (len(tracks) >= 20 and len(tracks) > 3 * file_count
                and not same_name(release.title, tag_album)):
            log.info("box-set guard: %s (%d tracks) rejected for %s (%d files)",
                     release.title, len(tracks), directory.name, file_count)
            continue
        pairs = matching.match_tracks(tracks, rows)
        if pairs:
            evaluated.append((release, tracks, pairs))
            # stop only on a PERFECT fit: a complete 10-track standard
            # edition must not preempt the 18-track deluxe that would
            # link every file in the folder
            if _fits(release, tracks, pairs, file_count):
                break
    return evaluated


async def _escalate(session, catalog: CatalogClient, discogs: DiscogsClient | None,
                    spotify: SpotifyClient | None, artist: Artist, tag_artist: str,
                    tag_album: str, rows: list, file_count: int,
                    best_linked: int) -> tuple[Release, list, dict] | None:
    """Every other source and edition, for a folder Tier 1 could not settle."""
    tag_titles = [row.tag_title or Path(row.path).stem for row in rows][:15]
    external = await albumsearch.resolve_external_ids(session, artist)
    fallback = await albumsearch.album_search_fallback(
        session, catalog, discogs, spotify, artist, external,
        tag_artist, tag_album, tag_titles, rows, file_count, best_linked,
    )
    if fallback is None:
        return None
    try:
        tracks = await ensure_tracks(session, fallback)
    except DiscographyError:
        return None
    pairs = matching.match_tracks(tracks, rows)
    return (fallback, tracks, pairs) if pairs else None


async def _confirm_edition(session, discogs: DiscogsClient | None, release: Release,
                           releases: list[Release], tag_artist: str, tag_album: str,
                           rows: list, tracks: list, pairs: dict) -> bool:
    matched = len(pairs)
    edition = await editions.confirm_edition(
        session, discogs, release, releases, tag_artist, tag_album, matched, rows)
    if edition is None:
        return False
    # the folder IS a full edition: rows beyond it are deleted outright — in
    # the edition we hold they do not exist
    for track in tracks:
        if track.id not in pairs:
            await session.delete(track)
    release.track_count = matched
    log.info("edition confirmed via %s: %s complete at %d — dropped %d extra rows",
             edition.evidence, release.title, matched, len(tracks) - matched)
    return True
