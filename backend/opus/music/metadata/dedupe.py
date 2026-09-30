"""Release identity across sources: which existing row a candidate IS (by
source id, then by title), which two rows are the same record, and what a
secondary source may change on a row it did not create. Title similarity alone
never decides — track counts and years corroborate, and identity follows the
files. A merged release keeps the earliest known year: the Discogs master year
is the ORIGINAL release year, while streaming dates are often reissues."""

from rapidfuzz import fuzz
from sqlalchemy import select

from opus.models import MusicFile, Release, ReleaseStatus, Track
from opus.music.textnorm import norm, same_name

TITLE_MATCH_THRESHOLD = 90


async def _merge_legacy_duplicates(session, releases: list[Release]) -> int:
    return (await _fold_discogs_only(session, releases)
            + await _dissolve_ghosts(session, releases))


async def _fold_discogs_only(session, releases: list[Release]) -> int:
    """Collapse discogs-only rows whose title matches a deezer-backed release
    (created before cross-source merging knew about them). The duplicate is
    deleted only when it holds no linked files."""
    merged = 0
    deezer_backed = [r for r in releases if r.deezer_id is not None]
    for release in list(releases):
        if (release.deezer_id is not None or release.spotify_id is not None
                or release.discogs_id is None or release.status != ReleaseStatus.NONE):
            continue
        best = _match_by_title(deezer_backed, release.title)
        if (best is None or best.discogs_id is not None
                or not _corroborated(release, best)
                or await _holds_files(session, release)):
            continue
        best.discogs_id = release.discogs_id
        if best.record_type is None:
            best.record_type = release.record_type
        _keep_earliest_year(best, release.release_date)
        await session.delete(release)
        releases.remove(release)
        merged += 1
    return merged


async def _dissolve_ghosts(session, releases: list[Release]) -> int:
    """A FILE-LESS deezer-backed duplicate of a row that holds files: identity
    follows the files, the ghost row dissolves (a mis-merge un-glue leaves
    exactly this shape behind)."""
    merged = 0
    for release in list(releases):
        if (release.deezer_id is None or release.status in _ACTIVE
                or await _holds_files(session, release)):
            continue
        holder = await _holder_of(session, release, releases)
        if holder is None:
            continue
        await _move_ids(session, release, holder)
        await session.delete(release)
        releases.remove(release)
        merged += 1
    return merged


_ACTIVE = (ReleaseStatus.WANTED, ReleaseStatus.SEARCHING, ReleaseStatus.DOWNLOADING)


async def _holder_of(session, ghost: Release, releases: list[Release]) -> Release | None:
    for other in releases:
        if (other is not ghost and other.deezer_id is None
                and fuzz.token_set_ratio(norm(ghost.title), norm(other.title)) >= TITLE_MATCH_THRESHOLD
                and _corroborated(ghost, other)
                and await _holds_files(session, other)):
            return other
    return None


def _corroborated(a: Release, b: Release) -> bool:
    # same title is NOT the same album when the tracklists differ (the
    # Australian and international "High Voltage" are different records)
    if a.track_count and b.track_count and a.track_count != b.track_count:
        return False
    # nor when a near-title pair lacks year corroboration — years apart
    # ("The Album" 1977 vs "The Albums" 2008 box) or unknown on either
    # side; plural traps score ~95 on tokens
    if same_name(a.title, b.title):
        return True
    year_a, year_b = _year(a.release_date), _year(b.release_date)
    return year_a is not None and year_b is not None and abs(year_a - year_b) <= 1


async def _holds_files(session, release: Release) -> bool:
    return (await session.execute(select(
        select(MusicFile.id)
        .join(Track, Track.id == MusicFile.track_id)
        .where(Track.release_id == release.id)
        .exists()
    ))).scalar()


async def _move_ids(session, ghost: Release, holder: Release):
    deezer, spotify, wikidata = ghost.deezer_id, ghost.spotify_id, ghost.wikidata_id
    # the ghost's unique ids must reach the DB as freed before they
    # move, or releases_deezer_id_key fires inside the same flush
    ghost.deezer_id = ghost.spotify_id = ghost.wikidata_id = None
    await session.flush()
    holder.deezer_id = deezer
    if holder.spotify_id is None:
        holder.spotify_id = spotify
    if holder.wikidata_id is None:
        holder.wikidata_id = wikidata


def _find_by_source_id(releases: list[Release], cand: dict) -> Release | None:
    if cand["source"] == "deezer":
        return next((r for r in releases if r.deezer_id == cand["external_id"]), None)
    if cand["source"] == "discogs":
        return next((r for r in releases if r.discogs_id == int(cand["external_id"])), None)
    return next((r for r in releases if r.spotify_id == cand["external_id"]), None)


def _fill_source_id(release: Release, cand: dict) -> bool:
    if cand["source"] == "deezer" and release.deezer_id is None:
        release.deezer_id = cand["external_id"]
        return True
    if cand["source"] == "discogs" and release.discogs_id is None:
        release.discogs_id = int(cand["external_id"])
        return True
    if cand["source"] == "spotify" and release.spotify_id is None:
        release.spotify_id = cand["external_id"]
        return True
    return False


def _year(date: str | None) -> int | None:
    if date and date[:4].isdigit():
        return int(date[:4])
    return None


def _apply_secondary_info(release: Release, cand: dict):
    """Discogs knows the original era: type untyped releases and keep the
    earliest known year (streaming dates are often reissues). A Wikidata-
    resolved release keeps its P577 date — that source outranks Discogs."""
    if cand["source"] == "deezer":
        # Deezer is the naming canon where Wikidata has not ruled: the same
        # title in different casing (one catalogue's 'DVOJKO' vs 'Dvojko') follows it
        if (release.wikidata_id is None and cand["title"]
                and same_name(release.title, cand["title"])
                and release.title != cand["title"]):
            release.title = cand["title"]
        # a full Deezer date may restore precision lost to a year-only value
        cand_date, current = cand.get("release_date"), release.release_date
        if (cand_date and len(cand_date) > 4 and current
                and len(current) == 4 and cand_date[:4] == current):
            release.release_date = cand_date
        return
    if cand["source"] != "discogs":
        return
    if release.record_type is None and cand.get("record_type"):
        release.record_type = cand["record_type"]
    if release.wikidata_id is not None:
        return
    _keep_earliest_year(release, cand.get("release_date"))


def _keep_earliest_year(release: Release, date: str | None):
    year, current = _year(date), _year(release.release_date)
    if year is not None and (current is None or year < current):
        release.release_date = str(year)


def _match_by_title(releases: list[Release], title: str) -> Release | None:
    best, best_score = None, 0.0
    for release in releases:
        score = fuzz.token_set_ratio(norm(title), norm(release.title))
        if score > best_score:
            best, best_score = release, score
    return best if best_score >= TITLE_MATCH_THRESHOLD else None
