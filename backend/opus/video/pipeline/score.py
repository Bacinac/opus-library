"""What a release is worth: resolution, source, sound, HDR, size for its
length, and whether it is the edition asked for."""

from guessit import guessit

from opus.video.channels.base import Candidate
from opus.settings_store import RuntimeConfig


RESOLUTION_ORDER = ["2160p", "1080p", "720p", "480p"]
SUBS_MARKERS = ("multi", "subs", "subbed", "dual", "multisubs", "vost")

# How the picture was got, which after resolution is the largest thing a release
# name says about it. A remux is the disc; a rip of a stream is what somebody's
# encoder made of it; a camera is a camera. This went unweighed, so a CAM and a
# Blu-ray remux of the same film scored the same.
SOURCE_RANK = {
    "remux": 90, "ultra hd blu-ray": 75, "blu-ray": 70, "web-dl": 55, "web": 50,
    "webrip": 35, "hdtv": 20, "dvd": 15, "tv": 10, "vhs": -50,
    "telecine": -150, "telesync": -180, "camera": -250, "workprint": -200,
}
# What the film sounds like. Ranked, never required: only about half of release
# names say anything about the audio at all, so demanding Atmos would throw away
# the half that simply does not mention what it has.
AUDIO_RANK = {
    "dts:x": 55, "dolby atmos": 55, "dolby truehd": 45, "dts-hd": 40,
    "flac": 35, "pcm": 35, "dts": 25, "dolby digital plus": 20,
    "dolby digital": 12, "aac": 8, "opus": 5, "mp3": 0,
}
CHANNEL_RANK = {"7.1": 20, "6.1": 16, "5.1": 12, "2.0": 2, "1.0": -5}
HDR_RANK = {"dolby vision": 25, "hdr10+": 20, "hdr10": 15, "hdr": 12}
# A release of an English-language film dubbed into another language is a
# different master with a different running time, and the subtitles that go with
# it are for that master. Planet of the Apes arrived as German.DTS.DL — eighteen
# gigabytes of MPEG-2 — and its Croatian subtitle was fifteen percent too long.
# Penalised rather than refused: better a dubbed copy than none, if it is all
# there is.
FOREIGN_EDITION_PENALTY = -150


def _listed(parsed, key: str) -> list[str]:
    """guessit answers with a value or a list of them, depending on how much the
    name said; both are the same question."""
    value = parsed.get(key)
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v).strip().lower() for v in items]


def _source_score(parsed) -> float:
    """Remux and Rip are qualifiers guessit hangs off `other`, not sources of
    their own: `Web` plus `Rip` is a re-encode of a stream, `Blu-ray` plus
    `Remux` is the disc untouched."""
    source = (str(parsed.get("source") or "")).strip().lower()
    other = _listed(parsed, "other")
    if "remux" in other:
        return SOURCE_RANK["remux"]
    if source == "web" and "rip" in other:
        return SOURCE_RANK["webrip"]
    if source == "blu-ray" and "rip" in other:
        return SOURCE_RANK["webrip"]
    return SOURCE_RANK.get(source, 0)


def _audio_score(parsed) -> float:
    codecs = _listed(parsed, "audio_codec")
    best = max((AUDIO_RANK.get(c, 0) for c in codecs), default=0)
    # DTS-HD Master Audio and plain DTS-HD are not the same thing
    if "master audio" in _listed(parsed, "audio_profile"):
        best = max(best, AUDIO_RANK["dts-hd"] + 5)
    channels = str(parsed.get("audio_channels") or "")
    return best + CHANNEL_RANK.get(channels, 0)


def _surround_score(parsed) -> float:
    """Sound that comes from more than in front of you.

    The channel count already counts for something in the audio score; this is
    the sentence "go out of your way for it" — worth stepping over a stereo
    release for, and deliberately smaller than a resolution, because a 5.1 track
    on a 720p rip is not the trade anybody meant. A release whose title says
    nothing about its channels says nothing here either."""
    channels = str(parsed.get("audio_channels") or "")
    front = channels.split(".")[0]
    if not front.isdigit():
        return 0.0
    return 40.0 if int(front) > 2 else -40.0


def _hdr_score(parsed, resolution: str) -> float:
    """Only where it can be shown: an HDR grade on a 720p rip is a word in a
    filename."""
    if resolution not in ("2160p", "1080p"):
        return 0.0
    return max((HDR_RANK.get(o, 0) for o in _listed(parsed, "other")), default=0)


def _bitrate_score(config: RuntimeConfig, c: Candidate, resolution: str,
                   runtime_s: float | None) -> float:
    """What the release spends per second of picture, against what this house
    asks for at that resolution.

    Size alone is not a judgement — eight gigabytes is a remuxed episode and a
    bad film — but size over running time is, and both are known before anything
    is fetched. Under the floor the resolution is a claim the file cannot pay
    for; over the ceiling it is disk spent past what gets watched."""
    size = c.payload or c.size
    if not runtime_s or not size or resolution not in RESOLUTION_ORDER:
        return 0.0
    mbps = size * 8 / runtime_s / 1_000_000
    floor = config.rates("bitrate_floor").get(resolution)
    ceiling = config.rates("bitrate_ceiling").get(resolution)
    c.parsed["_mbps"] = round(mbps, 1)
    if floor and mbps < floor:
        return -250 * min(1.0, (floor - mbps) / floor)
    if ceiling and mbps > ceiling:
        return -40 * min(2.0, (mbps - ceiling) / ceiling)
    return 40.0


def _foreign_edition(parsed, wanted: list[str]) -> bool:
    """Whether the name says this carries somebody else's language and ONLY
    that.

    `mul`, and the Dual Audio marker guessit reads out of DL and DUAL, are not
    noise to be filtered off before the question is asked — they are the answer.
    Planet of the Apes came as `German.DTS.DL`, which parses as German AND
    multi, and the file turned out to hold German and English both. Dropping
    `mul` first read that as a German-only release and docked it a hundred and
    fifty points for carrying exactly what was wanted."""
    languages = _listed(parsed, "language")
    if "mul" in languages or "dual audio" in _listed(parsed, "other"):
        return False
    known = [l for l in languages if l != "und"]
    return bool(known) and not any(l in wanted for l in known)


def score_candidate(config: RuntimeConfig, c: Candidate, *, profile: str = "",
                    runtime_s: float | None = None) -> float:
    c.parsed = dict(guessit(c.title))
    resolution = str(c.parsed.get("screen_size", ""))
    score = 0.0
    score += _resolution_score(resolution, profile or config.get("movie_quality_profile"))
    score += _source_score(c.parsed)
    score += _audio_score(c.parsed)
    score += _bitrate_score(config, c, resolution, runtime_s)
    if config.bool("prefer_hdr"):
        score += _hdr_score(c.parsed, resolution)
    if config.bool("prefer_surround"):
        score += _surround_score(c.parsed)
    if _foreign_edition(c.parsed, ["en"] + config.langs()):
        score += FOREIGN_EDITION_PENALTY

    if c.declared == "unreadable":
        # it says it holds neither picture nor an archive with picture in it
        score -= 300

    if c.subs_hint or any(marker in c.title.lower() for marker in SUBS_MARKERS):
        score += 15
    score += _seeders_score(c)
    score += _protocol_score(config.get("protocol_preference"), c.protocol)
    c.score = score
    return score


def _resolution_score(resolution: str, profile: str) -> float:
    if profile == "any":
        return ((len(RESOLUTION_ORDER) - RESOLUTION_ORDER.index(resolution)) * 20
                if resolution in RESOLUTION_ORDER else 0)
    if resolution == profile:
        return 100
    if resolution in RESOLUTION_ORDER and profile in RESOLUTION_ORDER:
        # distance below the profile is better than above (no gratuitous 4K
        # when 1080p was asked for)
        diff = RESOLUTION_ORDER.index(resolution) - RESOLUTION_ORDER.index(profile)
        return 60 - abs(diff) * 20 - (10 if diff < 0 else 0)
    return 0


def _seeders_score(c: Candidate) -> float:
    if c.protocol != "torrent":
        return 0
    seeders = c.seeders or 0
    # nobody seeding is a dead torrent
    return -200 if seeders == 0 else min(seeders, 50)


def _protocol_score(preference: str, protocol: str) -> float:
    """A tie-breaker, and nothing more: weighed against picture and sound it
    lets a 480p camera rip on the preferred protocol beat a remux."""
    if (preference, protocol) in (("usenet_first", "usenet"), ("torrent_first", "torrent")):
        return 20
    return 0


def candidate_json(c: Candidate) -> dict:
    """Serialize a scored candidate for the interactive release picker. The
    opaque `ref` (download URL / magnet) round-trips back on grab so the grab
    is stateless — no server-side candidate cache to expire."""
    lowered = c.title.lower()
    return {
        "channel": c.channel,
        "title": c.title,
        "size": c.size,
        "protocol": c.protocol,
        "seeders": c.seeders,
        "indexer": c.indexer,
        "guid": c.guid,
        "score": round(c.score, 1),
        "resolution": str(c.parsed.get("screen_size") or ""),
        "source": str(c.parsed.get("source") or ""),
        "video_codec": str(c.parsed.get("video_codec") or ""),
        "subs_hint": any(marker in lowered for marker in SUBS_MARKERS),
        "audio_codec": ", ".join(_listed(c.parsed, "audio_codec")),
        "audio_channels": str(c.parsed.get("audio_channels") or ""),
        "hdr": ", ".join(o for o in _listed(c.parsed, "other") if o in HDR_RANK),
        "mbps": c.parsed.get("_mbps"),
        "age_days": c.age_days,
        "payload": c.payload,
        "declared": c.declared,
        "ref": c.ref,
    }
