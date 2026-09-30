"""Whether a subtitle belongs to the file it sits beside.

A provider search answers about the FILM, not about the copy of it on this
disk, and the two are routinely different things: an extended cut, a PAL
transfer running four percent fast, a broadcast master with the ad breaks put
back. Such a subtitle is not mistimed — it is a subtitle of something else, and
no re-sync rescues it, because the lines themselves are in other places.

The tell is cheap and needs no reference: how far the last cue is from the end
of the picture. Dialogue stops before the credits do, so a subtitle written for
this file lands a little short of the running time and never past it. Running
past means a longer cut; falling far short means a shorter one, or half a file.

Planet of the Apes (2001) is the case this was written for: 7198 seconds of
film with a Croatian subtitle whose last cue is at 8254, a ratio of 1.15, adrift
from the first minute."""

import re
from dataclasses import dataclass
from pathlib import Path

# past the end of the picture at all is another cut; short of it by a fifth is
# either another cut or a subtitle that stops half way
MAX_RATIO = 1.02
MIN_RATIO = 0.80

FITS = "fits"
MISMATCH = "mismatch"
UNKNOWN = "unknown"

# srt and vtt say when a cue ends after the arrow, in milliseconds
SRT_CUE_END = re.compile(r"-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")
# ass and ssa put it second on the Dialogue line, in centiseconds
ASS_CUE_END = re.compile(r"^Dialogue:[^,]*,[^,]*,\s*(\d+):(\d\d):(\d\d)[.,](\d{1,2})", re.M)


@dataclass(frozen=True)
class Fit:
    """`ratio` is the last cue over the running time; UNKNOWN means the question
    could not be asked, which is not the same as answered no."""

    verdict: str
    ratio: float | None
    last_cue_s: float | None

    @property
    def mismatch(self) -> bool:
        return self.verdict == MISMATCH


def _stamp(h: str, m: str, s: str, frac: str, places: int) -> float:
    return (int(h) * 3600 + int(m) * 60 + int(s)
            + int(frac.ljust(places, "0")) / 10 ** places)


def last_cue_seconds(path: str | Path) -> float | None:
    """Where the subtitle stops talking. The furthest cue rather than the last
    one written: cues out of order happen when two files are merged, and the
    end of the file is then not the end of the dialogue."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ends = [_stamp(*m.groups(), 3) for m in SRT_CUE_END.finditer(text)]
    if not ends:
        ends = [_stamp(*m.groups(), 2) for m in ASS_CUE_END.finditer(text)]
    return max(ends) if ends else None


def check(path: str | Path, duration_s: float | None) -> Fit:
    last = last_cue_seconds(path)
    if last is None or not duration_s or duration_s <= 0:
        return Fit(UNKNOWN, None, last)
    ratio = last / duration_s
    return Fit(FITS if MIN_RATIO <= ratio <= MAX_RATIO else MISMATCH, ratio, last)


def describe(fit: Fit, duration_s: float | None) -> str:
    """The reason, for a log line that has to be read months later."""
    if fit.last_cue_s is None:
        return "no cue timings in the file"
    if fit.ratio is None:
        return f"last cue {fit.last_cue_s:.0f}s, running time unknown"
    return (f"last cue {fit.last_cue_s:.0f}s against {duration_s:.0f}s of picture, "
            f"ratio {fit.ratio:.2f}")
