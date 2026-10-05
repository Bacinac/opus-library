"""Ground truth about a video file: ffprobe enumerates the actual embedded
streams; sidecar subtitle files are matched by name. Release-name markers are
only a search-scoring hint — acceptance is decided here."""

import asyncio
import json
import re
from pathlib import Path

from babelfish import Error as LanguageError
from babelfish import Language

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".webm", ".wmv"}
SUB_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

# ISO 639-2/B (and common informal tags) → ISO 639-1
# spellings no standard lists any more: retired bibliographic codes, and the
# language's own name for itself
LANG_ALIASES = {"cro": "hr", "scr": "hr", "hrvatski": "hr", "scc": "sr", "ser": "sr"}
# name tokens that describe a subtitle track rather than name its language:
# "Movie.hr.hi.srt" is Croatian for the hard of hearing
SUB_MODIFIERS = {"hi", "sdh", "cc", "forced", "default", "foreign"}
# what this module writes beside a video itself, which is never a sidecar to import
OWN_VTT = ".opus.vtt"


class ProbeError(Exception):
    pass


def norm_lang(tag: str | None) -> str:
    """ISO 639-1 for a tag as files write it: a two- or three-letter code, either
    three-letter form, or an English name; "und" for anything else. A three-letter
    code cut to its first two letters is wrong more often than not — swe is
    Swedish, sw is Swahili, and chi, por and dut are not ch, po and du."""
    said = (tag or "").strip().lower()
    if said in LANG_ALIASES:
        return LANG_ALIASES[said]
    for read in (Language.fromalpha2, Language.fromalpha3b, Language.fromalpha3t,
                 lambda name: Language.fromname(name.title())):
        try:
            return read(said).alpha2
        except (LanguageError, KeyError, ValueError):
            continue
    return "und"


_SRT_NOISE = re.compile(r"<[^>]+>|\{[^}]+\}")
# constrain detection to plausible subtitle languages so a hard English sample
# is not misfiled as e.g. Afrikaans
_LANGID_LANGS = [
    "en", "hr", "sr", "bs", "sl", "de", "fr", "es", "it", "pt", "nl", "ru",
    "pl", "cs", "sk", "hu", "ro", "sv", "no", "da", "fi", "el", "tr", "uk", "bg",
]
_langid_ready = False


def detect_srt_lang(path: str | Path, sample_chars: int = 4000) -> str | None:
    """Guess a sidecar subtitle's language from its text — for files with no
    language tag in the name (e.g. a YTS bare '.srt', which is English).
    Returns an ISO 639-1 code or None when it can't tell."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parts: list[str] = []
    total = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        line = _SRT_NOISE.sub("", line)
        if line:
            parts.append(line)
            total += len(line)
            if total >= sample_chars:
                break
    sample = " ".join(parts)
    if len(sample) < 40:
        return None
    import py3langid
    global _langid_ready
    if not _langid_ready:
        py3langid.set_languages(_LANGID_LANGS)
        _langid_ready = True
    lang, _score = py3langid.classify(sample)
    return norm_lang(lang)


def _rate(value: str | None) -> float | None:
    """ffprobe writes a frame rate as a fraction — 24000/1001 for what everyone
    calls 24. A television is asked for a number, so it is turned into one here
    and not in three places downstream. A rate of zero is ffprobe saying it does
    not know, which is not the same as a file that runs at zero frames."""
    if not value or "/" not in value:
        return None
    top, _, bottom = value.partition("/")
    try:
        rate = int(top) / int(bottom)
    except (ValueError, ZeroDivisionError):
        return None
    return round(rate, 3) if rate > 0 else None


async def probe_file(path: str | Path) -> dict:
    """ffprobe a video file. Returns {container, video_codec, width, height,
    duration_s, audio_langs, streams:[...], subtitle_streams:[{lang, codec, forced}]}.

    `streams` is everything the file holds by way of picture and sound, one entry
    each. The probe used to keep a de-duplicated list of audio LANGUAGES and drop
    the rest, so a release with English TrueHD 7.1 and English stereo AC3 came out
    as the single word `en` — and anything that wanted to choose between them had
    to open the file again. The run is the same; only the throwing away is gone."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", "-show_chapters", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed for {path}: {err.decode(errors='replace').strip()}")
    return _read_probe(Path(path).suffix.lstrip("."), json.loads(out))


def _read_probe(container: str, data: dict) -> dict:
    result = {"container": container, "video_codec": "",
              "width": None, "height": None, "duration_s": None,
              "audio_langs": [], "streams": [], "subtitle_streams": []}
    try:
        result["duration_s"] = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    counts = {"video": 0, "audio": 0}
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        tags = s.get("tags", {}) or {}
        if kind in ("video", "audio"):
            result["streams"].append(_picture_or_sound(s, kind, counts[kind], tags))
            counts[kind] += 1
        if kind == "video" and not result["video_codec"]:
            result["video_codec"] = s.get("codec_name", "")
            result["width"], result["height"] = s.get("width"), s.get("height")
        elif kind == "audio":
            lang = norm_lang(tags.get("language"))
            if lang not in result["audio_langs"]:
                result["audio_langs"].append(lang)
        elif kind == "subtitle":
            result["subtitle_streams"].append({
                "lang": norm_lang(tags.get("language")),
                "codec": s.get("codec_name", ""),
                "forced": bool(s.get("disposition", {}).get("forced")),
            })
    result["chapters"] = _chapters(data)
    return result


def _picture_or_sound(s: dict, kind: str, position: int, tags: dict) -> dict:
    disposition = s.get("disposition", {}) or {}
    return {
        "kind": kind,
        # the number within its kind, which is what -map 0:a:N wants
        "position": position,
        "codec": s.get("codec_name", "") or "",
        "profile": (s.get("profile") or "")[:48] or None,
        "lang": norm_lang(tags.get("language")),
        "title": (tags.get("title") or "")[:200],
        "channels": s.get("channels"),
        "width": s.get("width"),
        "height": s.get("height"),
        "bit_depth": s.get("bits_per_raw_sample") and int(s["bits_per_raw_sample"]),
        "frame_rate": _rate(s.get("avg_frame_rate") or s.get("r_frame_rate")),
        "color_transfer": (s.get("color_transfer") or "")[:24],
        "color_primaries": (s.get("color_primaries") or "")[:24],
        "default": bool(disposition.get("default")),
        "forced": bool(disposition.get("forced")),
    }


def _chapters(data: dict) -> list[dict]:
    chapters = []
    for i, chapter in enumerate(data.get("chapters", []) or []):
        try:
            start = float(chapter.get("start_time"))
        except (TypeError, ValueError):
            continue
        chapters.append({
            "position": i,
            "start_s": round(start, 3),
            "title": ((chapter.get("tags") or {}).get("title") or "")[:200],
        })
    return chapters


def sidecar(video: Path, subtitle: Path) -> dict:
    """What a subtitle file's name says about it. After the video's own name
    (`Show.S01E05.hr.forced.srt`) the language is the last word once the words
    describing the track are set aside; a name of its own (`Subs/2_English.srt`)
    is read by its last word. A bare .srt names nothing, so its text is asked."""
    stem = subtitle.name[: -len(subtitle.suffix)]
    if stem.lower().startswith(video.stem.lower()):
        words = [w for w in stem[len(video.stem):].split(".") if w]
    else:
        words = [w for w in re.split(r"[._\s-]+", stem) if w]
    forced = False
    while words and words[-1].lower() in SUB_MODIFIERS:
        forced = forced or words[-1].lower() == "forced"
        words.pop()
    lang = norm_lang(words[-1]) if words else "und"
    if lang == "und" and subtitle.suffix.lower() == ".srt":
        lang = detect_srt_lang(subtitle) or "und"
    return {"path": str(subtitle), "lang": lang,
            "format": subtitle.suffix.lstrip(".").lower(), "forced": forced}


def _subtitle_file(path: Path) -> bool:
    return (path.suffix.lower() in SUB_EXTENSIONS and not path.name.lower().endswith(OWN_VTT)
            and path.is_file())


def sidecar_subs(video_path: str | Path) -> list[dict]:
    """Every subtitle in a download's folder, subfolders included: beside a
    release the only video is this one, so everything around it is its own."""
    video = Path(video_path)
    return [sidecar(video, c) for c in sorted(video.parent.rglob("*")) if _subtitle_file(c)]


def own_sidecars(video_path: str | Path) -> list[dict]:
    """The subtitles beside a file that already sits in the library. In a season
    folder the neighbours are other episodes, so only names that extend this
    video's own count."""
    video = Path(video_path)
    return [sidecar(video, c) for c in sorted(video.parent.iterdir())
            if _subtitle_file(c) and c.name.lower().startswith(f"{video.stem.lower()}.")]


async def declared_duration(path: str | Path) -> float | None:
    """What the container's header says it holds, without reading the data."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return None


async def tail_ok(path: str | Path, seconds: int = 20) -> bool:
    """Whether the file really runs to the end it claims.

    A container states its duration in the header before a single frame is
    written, so a copy cut off half way still reports the full running time and
    probes clean. Seeking relative to the end settles it: on a whole file the
    last stretch decodes, and on a torn one the seek lands past where the data
    stops and nothing comes out at all.

    What is counted is how much decoded, not what was printed. ffmpeg complains
    about non-monotonic timestamps on plenty of perfectly good releases, and
    treating any output as damage would throw away five gigabytes over a muxer's
    opinion. The window has to come out whole, too: a file missing only its last
    tenth still decodes several seconds from where the seek lands, and a
    stretch that merely starts is not a stretch that finishes."""
    declared = await declared_duration(path)
    if declared is None:
        return False
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-nostats", "-progress", "pipe:1",
        "-sseof", f"-{seconds}", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-f", "null", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return False
    decoded = 0.0
    for line in out.decode(errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key == "out_time_us" and value.strip().isdigit():
            decoded = int(value) / 1_000_000
    return decoded >= min(float(seconds), declared) - 1.0


def find_video_files(directory: str | Path) -> list[Path]:
    """All plausible video files under a completed download folder, largest
    first; samples and extras are filtered by name."""
    root = Path(directory)
    if root.is_file():
        return [root] if root.suffix.lower() in VIDEO_EXTENSIONS else []
    files = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        and ".opus-imports" not in p.parts
        and "sample" not in p.name.lower()
    ]
    return sorted(files, key=lambda p: p.stat().st_size, reverse=True)
