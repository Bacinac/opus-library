"""File-to-track matching: which file on disk is which track of a release.
The tag title decides, the filename is the fallback, track number and duration
only corroborate — the rules a library of real rips (mistagged, cross-tagged,
renamed) forced into existence. Pure functions over already-loaded rows: no DB
and no network, so a candidate edition can be scored before anything is
written."""

import logging
from pathlib import Path

from opus.models import MusicFile
from opus.music.textnorm import norm, same_name, title_score

log = logging.getLogger("opus.libimport")

TRACK_MATCH_THRESHOLD = 65


def _match_score(title: str, row: MusicFile, position: int | None = None,
                 duration: int | None = None) -> float:
    """Tag title is the primary key — exact normalized match wins outright —
    with the filename as fallback. Track number and duration only CORROBORATE
    an already-plausible title match: a small nudge that disambiguates between
    similar-titled rows (live vs studio, a song and its reprise), never rescues
    a title that does not match."""
    if row.tag_title and norm(row.tag_title).strip() == norm(title).strip():
        base = 100.0
    elif row.tag_title:
        base = max(title_score(title, row.tag_title),
                   title_score(title, Path(row.path).stem))
    else:
        base = title_score(title, Path(row.path).stem)
    if base >= TRACK_MATCH_THRESHOLD:
        if position and row.tag_track == position:
            base = min(100.0, base + 4)
        if duration and row.duration_sec and abs(duration - row.duration_sec) <= 3:
            base = min(100.0, base + 4)
    return base


def match_tracks(tracks: list, rows: list[MusicFile]) -> dict[int, int]:
    """Pure matching pass: {track_id: file_row_id}, no links written — used to
    compare candidate editions before committing to one."""
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for track in tracks:
        best_row, best_score = None, 0.0
        for row in rows:
            if row.id in used:
                continue
            score = _match_score(track.title, row, track.position, track.duration_sec)
            if score > best_score:
                best_row, best_score = row, score
        if best_row is not None and best_score >= TRACK_MATCH_THRESHOLD:
            used.add(best_row.id)
            pairs[track.id] = best_row.id
    # rescue pass: a song can live under a different name entirely (the tag
    # says 'Dva', the catalog says 'Broj 2') — no title score can bridge that,
    # but an unfilled track and a leftover file agreeing on BOTH position and
    # duration are the same recording. Only leftovers pair here, so a title
    # mismatch never overrides a title match.
    for track in tracks:
        if track.id in pairs or not track.position or not track.duration_sec:
            continue
        for row in rows:
            if (row.id in used or row.tag_track != track.position
                    or not row.duration_sec
                    or abs(row.duration_sec - track.duration_sec) > 7):
                continue
            used.add(row.id)
            pairs[track.id] = row.id
            break
    _unswap_cross_tagged(tracks, rows, pairs)
    return pairs


def _duration_fits(track, row: MusicFile) -> bool:
    return bool(track.duration_sec and row.duration_sec
                and abs(track.duration_sec - row.duration_sec) <= 7)


def _unswap_cross_tagged(tracks: list, rows: list[MusicFile],
                         pairs: dict[int, int]) -> None:
    """A bad rip swaps the TITLE tags of two files; title-led matching then
    cross-links them (Reservoir Dogs: the 15s dialogue wearing the song's
    name). When two paired files each fit the OTHER track's duration and
    track number, and clearly not their own, the audio outvotes the written
    tag — swap the links in place."""
    row_by_id = {r.id: r for r in rows}
    paired = [(t, row_by_id[pairs[t.id]]) for t in tracks if t.id in pairs]
    for i in range(len(paired)):
        for j in range(i + 1, len(paired)):
            track_a, row_a = paired[i]
            track_b, row_b = paired[j]
            if not (_duration_fits(track_a, row_b) and _duration_fits(track_b, row_a)
                    and not _duration_fits(track_a, row_a)
                    and not _duration_fits(track_b, row_b)):
                continue
            if row_b.tag_track and row_b.tag_track != track_a.position:
                continue
            if row_a.tag_track and row_a.tag_track != track_b.position:
                continue
            pairs[track_a.id], pairs[track_b.id] = row_b.id, row_a.id
            paired[i], paired[j] = (track_a, row_b), (track_b, row_a)
            log.info("cross-tagged pair unswapped: %r <-> %r",
                     track_a.title, track_b.title)


def rank_release(pair, tag_album: str, file_count: int):
    score, release = pair
    # among score ties (plain vs Deluxe/Expanded — parens-strip makes both
    # 100), prefer the exact title, then the edition whose track count fits
    # the folder, then the shortest (base) title
    closeness = (
        -abs(release.track_count - file_count)
        if release.track_count else -999
    )
    return (
        score,
        same_name(release.title, tag_album),
        closeness,
        -len(release.title),
        release.deezer_id is not None,
    )


def eval_titles(candidate_titles: list[str], rows: list[MusicFile]) -> int:
    """Greedy count of folder files a candidate tracklist would link — the
    same measure match_tracks uses, computed BEFORE anything is created."""
    used: set[int] = set()
    matched = 0
    for title in candidate_titles:
        best_row, best_score = None, 0.0
        for row in rows:
            if row.id in used:
                continue
            score = _match_score(title, row)
            if score > best_score:
                best_row, best_score = row, score
        if best_row is not None and best_score >= TRACK_MATCH_THRESHOLD:
            used.add(best_row.id)
            matched += 1
    return matched


def songs_overlap(track_titles: list[str], songs: list[str]) -> int:
    """How many of the folder's songs a candidate tracklist actually carries —
    the content check that keeps 'Blues Brothers' from resolving to the homonym
    'Brothers In Blues', whose songs share nothing with the folder."""
    used: set[int] = set()
    hits = 0
    for song in songs:
        for i, title in enumerate(track_titles):
            if i in used:
                continue
            if title_score(song, title) >= TRACK_MATCH_THRESHOLD:
                used.add(i)
                hits += 1
                break
    return hits
