"""Getting an embedded subtitle out of the container, once.

A text subtitle inside an MKV is not something a browser can be handed: it has
to be demuxed first, and demuxing means reading the file — the whole file, since
subtitle packets are interleaved through it. On a Blu-ray remux that was measured
at 183 seconds, which is why a player asking for one at the moment somebody
pressed a button never showed anything.

So it is done at import, in one pass that writes every wanted language at once,
and the result sits beside the video as a .vtt for the rest of the file's life."""

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# what a browser will take; the rest are pictures and cannot become text
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "eia_608", "subviewer"}


def vtt_for(video: Path, lang: str, position: int) -> Path:
    """Beside the video, named so two tracks of one language do not collide."""
    return video.with_name(f"{video.stem}.{lang}.{position}.opus.vtt")


async def extract(video: Path, wanted: list[dict]) -> dict[int, str | None]:
    """One pass over the container, every wanted track written out.

    `wanted` is [{position, lang}] in ffmpeg's subtitle-stream numbering. Returns
    {position: path} for the ones that came out and {position: None} for the
    ones the container was read through and found empty; a position missing is
    one ffmpeg did not get through, worth another try. Extracting them one at a
    time would mean reading the file once per language, which is the difference
    between three minutes and a quarter of an hour."""
    if not wanted:
        return {}

    outputs: list[str] = []
    produced: dict[int, Path] = {}
    for track in wanted:
        target = vtt_for(video, track["lang"], track["position"])
        produced[track["position"]] = target
        outputs += ["-map", f"0:s:{track['position']}", "-f", "webvtt", "-y", str(target)]

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), *outputs,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("subtitle extract failed for %s: %s", video,
                    err.decode(errors="replace").strip()[:300])

    # a track that produced nothing is not reported as ready: an empty file on
    # disk would be offered in the menu and show nothing when chosen
    out: dict[int, str | None] = {}
    for position, target in produced.items():
        if target.exists() and target.stat().st_size > 16:
            out[position] = str(target)
        elif target.exists():
            target.unlink()
            if proc.returncode == 0:
                out[position] = None
    return out
