"""Asking the indexers for a film or an episode, and keeping only what is
that film or that episode."""

import logging
import re

from sqlalchemy import select

from opus.video.channels import prowlarr
from opus import acquire
from opus.acquire import AcquireError
from opus.video.channels.base import Candidate
from opus.models import Episode, VideoFile, Movie, Season
from opus.settings_store import RuntimeConfig
from opus.video.pipeline import score

log = logging.getLogger("opus.video.pipeline")


VIDEO_SUFFIXES = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".webm")
ARCHIVE_SUFFIXES = (".rar", ".zip", ".7z")
ARCHIVE_PART = re.compile(r"\.r\d\d$|\.part\d+\.rar$|\.\d{3}$")
# how many of the best-scoring releases are asked what they actually hold. Each
# one is a request, and the answer only changes the order near the top
INSPECT_DEPTH = 6


def _payload(files: list[dict]) -> tuple[int, str]:
    """Of everything the release declares, how many bytes are picture — and what
    shape the release is.

    The advertised size is not the film. A usenet post carries par2 recovery
    beside it, measured at 17% of the total on six of this library's releases
    and 2% on the rest, so scoring a rate against the advertised size overstates
    every usenet release by up to a fifth. And some declare no video at all
    because the film is inside a split archive, which is legitimate and worth
    knowing before rather than after."""
    video = sum(f.get("size") or 0 for f in files
                if f.get("name", "").lower().endswith(VIDEO_SUFFIXES)
                and "sample" not in f.get("name", "").lower())
    if video:
        return video, "video"
    packed = sum(f.get("size") or 0 for f in files
                 if f.get("name", "").lower().endswith(ARCHIVE_SUFFIXES)
                 or ARCHIVE_PART.search(f.get("name", "").lower()))
    if packed:
        return packed, "archive"
    return 0, "unreadable"


async def refine_candidates(config: RuntimeConfig, candidates: list[Candidate], *,
                            profile: str, runtime_s: float | None,
                            depth: int = INSPECT_DEPTH) -> list[Candidate]:
    """Ask the best few what they actually hold, then score them on the answer.

    Only the best few: every answer is a request, and a release that the name
    already ranks fortieth is not going to win on its file list."""
    for c in candidates[:depth]:
        try:
            declared = await acquire.inspect(config, c.ref["grab_ref"])
        except AcquireError as exc:
            log.debug("inspect failed for %s: %s", c.title, exc)
            continue
        files = declared.get("files") or []
        if not files:
            continue
        c.payload, c.declared = _payload(files)
        score.score_candidate(config, c, profile=profile, runtime_s=runtime_s)
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def _norm_name(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _numbers(parsed, key: str) -> list[int]:
    value = parsed.get(key)
    if isinstance(value, list):
        return [v for v in value if isinstance(v, int)]
    return [value] if isinstance(value, int) else []


def _release_is_of(c: Candidate, *, movie: Movie | None = None,
                   episode: Episode | None = None) -> bool:
    """Whether the release names the thing that was asked for.

    An indexer answers a query, not a question. Searching for a series and an
    episode number returns everything whose name carries those words, and one
    of those was an American Dad episode called "Not Particularly Desperate
    Housewives" — 1080p, MULTi, top of the list by score, and twenty-two
    minutes long. It downloaded, imported and shelved itself as the wanted
    episode without a single step objecting.

    Containment rather than equality, in both directions: a release is allowed
    to say more than the catalogue does (`Doctor Who 2005`) and less (`The
    Office` for `The Office US`). A season pack carries no episode number and
    is a legitimate answer; a number that disagrees is not."""
    wanted = _norm_name(movie.title if movie is not None else episode.season.series.title)
    found = _norm_name(str(c.parsed.get("title") or ""))
    if not found or not (found in wanted or wanted in found):
        return False
    if episode is not None:
        seasons = _numbers(c.parsed, "season")
        if seasons and episode.season.number not in seasons:
            return False
        numbers = _numbers(c.parsed, "episode")
        return not numbers or episode.number in numbers
    years = _numbers(c.parsed, "year")
    return not (movie.year and years) or abs(years[0] - movie.year) <= 1


async def _expected_runtime(session, *, movie: Movie | None = None,
                            episode: Episode | None = None) -> float | None:
    """How long the thing being searched for runs, in seconds — the denominator
    that turns a release's size into a rate.

    A film carries its running time from TMDB. An episode does not, and asking
    TMDB for one is a request per episode for a number the shelf already knows:
    the episodes of that series already on disk were all cut to the same length,
    so their median duration is what the next one will be. A series with nothing
    on disk yet has no answer, and gets none rather than a guess."""
    if movie is not None:
        return movie.runtime_min * 60 if movie.runtime_min else None
    durations = list((await session.execute(
        select(VideoFile.duration_s)
        .join(Episode, Episode.id == VideoFile.episode_id)
        .join(Season, Season.id == Episode.season_id)
        .where(Season.series_id == episode.season.series_id,
               VideoFile.duration_s.is_not(None)))).scalars())
    if len(durations) < 3:
        return None
    durations.sort()
    return durations[len(durations) // 2]


async def search_candidates(config: RuntimeConfig, *, movie: Movie | None = None,
                            episode: Episode | None = None,
                            session=None) -> list[Candidate]:
    if movie is not None:
        query = f"{movie.title} {movie.year}" if movie.year else movie.title
        category = prowlarr.CATEGORY_MOVIES
        profile = config.get("movie_quality_profile")
    else:
        series = episode.season.series
        query = f"{series.title} S{episode.season.number:02d}E{episode.number:02d}"
        category = prowlarr.CATEGORY_TV
        profile = config.get("tv_quality_profile")
    runtime_s = None
    if session is not None:
        runtime_s = await _expected_runtime(session, movie=movie, episode=episode)
    candidates = await prowlarr.search(config, query, category)
    for c in candidates:
        score.score_candidate(config, c, profile=profile, runtime_s=runtime_s)
    kept = [c for c in candidates if _release_is_of(c, movie=movie, episode=episode)]
    if len(kept) != len(candidates):
        log.info("search %r: %d of %d releases are of something else",
                 query, len(candidates) - len(kept), len(candidates))
    kept.sort(key=lambda c: c.score, reverse=True)
    return await refine_candidates(config, kept, profile=profile, runtime_s=runtime_s)
