"""What more than one video router needs: the shapes a page posts back, and the
file and status fragments both the movie and the episode views render.

A unit because each of these is used by two or more resource routers and must
read identically in all of them."""

import sqlalchemy as sa
from pydantic import BaseModel, field_validator

from opus.models import Movie, Subtitle, VideoFile
from opus.video.channels.base import Candidate
from opus.video.subtitles.policy import OVERRIDE_RE, effective_policy, evaluate, normal_override


class AddByTmdbId(BaseModel):
    tmdb_id: int


class ItemPatch(BaseModel):
    monitored: bool | None = None
    subtitle_override: str | None = None

    @field_validator("subtitle_override")
    @classmethod
    def _readable(cls, spec: str | None) -> str | None:
        if spec is None:
            return None
        spec = normal_override(spec)
        if spec and not OVERRIDE_RE.match(spec):
            raise ValueError("a subtitle override is 'none', or 'any:' or 'all:' with languages")
        return spec


class SeasonPatch(BaseModel):
    monitored: bool


class ReleaseSelection(BaseModel):
    """A release the user picked in the interactive picker, echoed back from
    the /releases listing (ref carries the download URL / magnet)."""

    channel: str
    title: str
    protocol: str
    size: int = 0
    seeders: int | None = None
    indexer: str = ""
    guid: str = ""
    score: float = 0.0
    ref: dict


def _candidate_from_selection(sel: ReleaseSelection) -> Candidate:
    return Candidate(
        channel=sel.channel, title=sel.title, size=sel.size,
        protocol=sel.protocol, seeders=sel.seeders, indexer=sel.indexer, guid=sel.guid,
        ref=sel.ref, score=sel.score,
    )


# What a frame has to reach to be called each of these, read across the frame and
# down it. Both, because a scope film is a 1080p or 2160p master with the top and
# bottom cut off it: `Ted Lasso` at 1920x960 and a 4K film at 3840x1632 are full
# height in neither, and asking the height alone called one of them 720p and 276
# of the other 1080p. Whichever way round the frame is, the tier it reaches on
# either axis is the tier it is.
_ACROSS = ((3000, "2160p"), (1800, "1080p"), (1200, "720p"), (700, "480p"))
_DOWN = ((1700, "2160p"), (1000, "1080p"), (700, "720p"), (400, "480p"))
_TIERS = ["480p", "720p", "1080p", "2160p"]


def _tier(measure: int, ladder: tuple) -> str:
    for least, name in ladder:
        if measure >= least:
            return name
    return ""


def _resolution(media: VideoFile) -> str:
    reached = [t for t in (_tier(media.width or 0, _ACROSS), _tier(media.height or 0, _DOWN)) if t]
    if reached:
        return max(reached, key=_TIERS.index)
    return f"{media.width}×{media.height}" if media.width and media.height else ""


def _sub_sort_key(s: dict) -> tuple:
    # required languages first (hr, en), then the rest alphabetically
    return (s["lang"] != "hr", s["lang"] != "en", s["lang"])


def _stream_json(stream) -> dict:
    return {
        "kind": stream.kind, "position": stream.position, "codec": stream.codec,
        "profile": stream.profile,
        "lang": stream.lang, "title": stream.title, "channels": stream.channels,
        "width": stream.width, "height": stream.height, "bit_depth": stream.bit_depth,
        "frame_rate": stream.frame_rate,
        "color_transfer": stream.color_transfer, "color_primaries": stream.color_primaries,
        "default": stream.default, "forced": stream.forced,
    }


def _file_json(media: VideoFile) -> dict:
    return {
        "duration_s": media.duration_s,
        "streams": [_stream_json(s) for s in media.streams],
        "resolution": _resolution(media),
        "video_codec": media.video_codec,
        "container": media.container,
        "size": media.size,
        "audio_langs": media.audio_langs or [],
        "subtitles": sorted(
            [{"lang": s.lang, "source": s.source, "forced": s.forced,
              "auto": s.auto_generated} for s in media.subtitles], key=_sub_sort_key),
    }


def _playback_json(media: VideoFile) -> dict:
    """Everything a player needs about the FILE, whatever the file happens to be
    an episode or a film of. The path is here and deliberately not in the
    listing: where a file sits is the library's business right up until somebody
    has to open it, and then it is the only thing that matters."""
    return {
        "duration_s": media.duration_s,
        "streams": [_stream_json(s) for s in media.streams],
        "path": media.path,
        "container": media.container,
        "video_codec": media.video_codec,
        "width": media.width,
        "height": media.height,
        "size": media.size,
        "audio_langs": media.audio_langs or [],
        "chapters": [{"position": c.position, "start_s": c.start_s, "title": c.title}
                     for c in media.chapters],
        "credits_s": media.credits_s,
        "intro_s": media.intro_s,
        "intro_end_s": media.intro_end_s,
        "subtitles": [
            {"id": s.id, "lang": s.lang, "source": s.source, "format": s.format,
             "forced": s.forced, "auto": s.auto_generated,
             # what a player can actually open: the sidecar, or the copy read
             # out of the container at import
             "path": s.vtt_path or s.path}
            for s in media.subtitles
        ],
    }


def _movie_status(config, movie: Movie, active: set[int]) -> dict:
    files = movie.files
    if files:
        subs = [s for f in files for s in f.subtitles]
        policy = effective_policy(config, movie.subtitle_override)
        result = evaluate(policy, subs, accept_auto=config.bool("accept_auto_subs"))
        status = "complete" if result.satisfied else "waiting_subtitles"
        return {"status": status, "missing_subs": list(result.missing),
                "present_subs": list(result.present), "replacing": movie.id in active}
    if movie.id in active:
        return {"status": "downloading", "missing_subs": [], "present_subs": []}
    return {"status": "wanted", "missing_subs": [], "present_subs": []}


def _badges(movie: Movie) -> dict:
    """The two facts worth seeing without opening anything: how big the picture
    is and what it is encoded with. Both sit on the file row, so a shelf of a
    hundred and thirty-four costs no extra query to label."""
    if not movie.files:
        return {"resolution": "", "codec": ""}
    media = movie.files[0]
    return {"resolution": _resolution(media), "codec": media.video_codec}


def overview_in(item, lang: str) -> str:
    """Croatian when it was asked for and TMDB had one, English otherwise. A
    reader who chose Croatian and is handed English has still been answered;
    one who is handed nothing has not."""
    if lang == "hr" and getattr(item, "overview_hr", ""):
        return item.overview_hr
    return item.overview


def _movie_json(movie: Movie, status: dict, lang: str = "en") -> dict:
    return {
        **_badges(movie),
        "id": movie.id, "tmdb_id": movie.tmdb_id, "imdb_id": movie.imdb_id,
        "title": movie.title, "original_title": movie.original_title,
        "year": movie.year, "overview": overview_in(movie, lang),
        "poster_url": movie.poster_url, "backdrop_url": movie.backdrop_url,
        "runtime_min": movie.runtime_min,
        "genres": movie.genres, "studios": movie.studios,
        "directors": movie.directors, "cast": movie.cast,
        "monitored": movie.monitored, "subtitle_override": movie.subtitle_override,
        **status,
    }


def held_langs(accept_auto: bool):
    """The distinct languages a group of subtitle rows carries, as one aggregate:
    the naive shape returns one row per subtitle, and this library holds twenty-one
    thousand of them to answer a question about three thousand items."""
    counted = Subtitle.lang.is_not(None)
    if not accept_auto:
        counted = sa.and_(counted, Subtitle.auto_generated.is_(False))
    return sa.func.array_agg(Subtitle.lang.distinct()).filter(counted)
