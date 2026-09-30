"""Putting a subtitle back on the picture, when somebody already knows it is the
right subtitle.

A subtitle can carry the right words at the wrong times: a transfer running at
twenty-five frames where the file runs at 23.976, a release whose distributor
logo is eight seconds longer, a rip that starts after the cold open. The words
are somebody's translation and worth keeping; only the numbers are wrong. alass
fixes that well — measured against a subtitle displaced by ten minutes, by a PAL
stretch, and by both at once, it put all three back to within the second and a
half that separates any Croatian subtitle from any English one, in under two
seconds and without reading the video.

WHAT IT CANNOT DO, and why nothing here calls it on its own:

It cannot tell whether the subtitle belongs to the film at all. Handed the
Croatian subtitle of a different film entirely, alass fits that onto the
reference just as obligingly — measured at 1.07, 1.21, 1.43 and 1.87 seconds
residual for four unrelated films, against 1.08 for the genuine one. A fitter
fits whatever it is given, and in doing so it destroys the evidence the
library's acceptance test reads: the running time stops disagreeing, the last
cue lands where it should, and a subtitle that was plainly wrong becomes one
that passes every check we have.

So this runs only where a person has already answered the question it cannot —
"this is the right translation, its timing is off" — and never inside an
acquisition loop, where identity is precisely what is in doubt.

THE REFERENCE IS THE CLOCK, NOT THE WORDS.

Aligning needs to know when somebody speaks, and nothing whatever about what
they say. That is a far cheaper question than it looks, and it is answered by
any subtitle track the container carries:

  - An extracted .vtt sitting beside the video, if the extraction pass has been
    there. Free, complete, no reading at all — but only 102 files in this
    library have one with real dialogue in it.
  - Failing that, the packet timestamps of ANY embedded track, read straight out
    of the container without decoding a frame. 2118 of 2198 files carry one.

That second line includes the picture tracks. A PGS or DVD subtitle can never
become text, which is why the extraction pass skips it — but its packets are
stamped with exactly when each caption appears, and measured against the text
track of the same file those stamps agree to the millisecond: 100% coverage,
median 0.00s. A track we could never read is a perfect clock.

The packets come in pairs, one to put a caption up and one to take it down; the
taking-down packet is a few bytes and the putting-up one is a picture, so the
median packet size separates them. On Veep S02E08 that turned 252 packets into
125 display events, which is exactly how many cues the text track has.

Cost is what the file's bitrate says it is — 50s for a 6.6 GB 1080p remux,
minutes for an MPEG2 one — against the three minutes a full extraction takes,
for twenty times as many files. It is read once per alignment, and alignment is
something a person asks for one subtitle at a time.

A reference track has to have real dialogue in it: a forced or signs-only track
is a handful of cues and aligns nothing — Napoleon's first English track holds
two."""

import logging
import re
import subprocess
import tempfile
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ALASS = "/usr/local/bin/alass"
CUE_START = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->")

# a track with fewer cues than this is signs and songs, not dialogue, and makes
# no reference
MIN_REFERENCE_CUES = 100
# how far a cue may sit from its nearest neighbour and still be read as the same
# line. Croatian and English split lines differently, so even a subtitle in
# perfect time measures around a second and a half
NEAR = 5.0
# a subtitle whose lines this much of the reference can find, this close, is
# already on the clock; aligning it again would only move cues about
IN_TIME_COVERAGE = 0.75
IN_TIME_DISTANCE = 2.0


@dataclass(frozen=True)
class Aligned:
    status: str              # aligned | unchanged | failed
    before: float = 0.0      # share of the reference that found a line, before
    after: float = 0.0       # and after
    distance: float = 0.0    # median distance of those that did, after
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("aligned", "unchanged")


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(str(ms).ljust(3, "0")[:3]) / 1000


def cue_starts(path: str | Path) -> list[float]:
    """When this subtitle speaks, in order. Reads srt and vtt alike."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sorted(_secs(*m.groups()) for m in CUE_START.finditer(text))


def agreement(reference: list[float], target: list[float]) -> tuple[float, float]:
    """How much of the reference finds a line near it (0..1), and how near.

    Both numbers are needed. Distance alone lies about a badly displaced
    subtitle: shift one by forty seconds and the handful of cues that still land
    near something read as a tight fit, while the ninety percent that found
    nothing are not counted at all. Coverage is what collapses, and it is the
    honest measure of being out of time.

    Neither says whether the subtitle belongs to the film — see the note at the
    top of this module."""
    found = []
    for r in reference:
        i = bisect_left(target, r)
        near = target[max(0, i - 1):i + 2]
        if near:
            best = min(abs(t - r) for t in near)
            if best < NEAR:
                found.append(best)
    if not found:
        return 0.0, float("inf")
    found.sort()
    return len(found) / len(reference), found[len(found) // 2]


def _glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


def _extracted_reference(video: Path) -> tuple[list[float], str]:
    """The fullest English track the extraction pass already wrote out."""
    best, name = [], ""
    for candidate in video.parent.glob(f"{_glob_escape(video.stem)}.en.*.opus.vtt"):
        cues = cue_starts(candidate)
        if len(cues) > len(best):
            best, name = cues, candidate.name
    return best, name


def _packet_reference(video: Path, stream: int, bitmap: bool) -> list[float]:
    """When each caption of one embedded track appears, from the container's
    packet stamps. Decodes nothing and works on picture tracks."""
    fields = "packet=pts_time,size" if bitmap else "packet=pts_time"
    try:
        run = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", f"s:{stream}",
             "-show_entries", fields, "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not read subtitle packets of %s: %s", video.name, exc)
        return []
    stamps: list[tuple[float, int]] = []
    for line in run.stdout.splitlines():
        parts = line.split(",")
        try:
            stamps.append((float(parts[0]), int(parts[1]) if bitmap else 0))
        except (ValueError, IndexError):
            continue
    if not bitmap:
        return sorted(t for t, _ in stamps)
    # a caption is put up by a packet carrying a picture and taken down by one
    # carrying almost nothing; only the putting-up is a cue
    sizes = sorted(s for _, s in stamps)
    cut = sizes[len(sizes) // 2]
    return sorted(t for t, s in stamps if s > cut)


def reference_cues(video: str | Path, tracks: list[dict]) -> tuple[list[float], str]:
    """The best clock this file can offer, and what it came from.

    `tracks` is [{stream_index, lang, format}] of the embedded subtitles, as the
    catalogue holds them. Any language will do — two subtitles of one film agree
    about when people speak whatever they say."""
    video = Path(video)
    cues, name = _extracted_reference(video)
    if len(cues) >= MIN_REFERENCE_CUES:
        return cues, name

    for track in sorted(tracks, key=lambda t: t.get("stream_index") or 0):
        if track.get("stream_index") is None:
            continue
        bitmap = (track.get("format") or "").lower() in BITMAP_CODECS
        cues = _packet_reference(video, track["stream_index"], bitmap)
        if len(cues) >= MIN_REFERENCE_CUES:
            return cues, f"stream s:{track['stream_index']} ({track.get('lang') or '?'}"\
                         f", {track.get('format') or '?'})"
    return [], ""


def _stamp(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(cues: list[float], dest: Path) -> None:
    """alass reads srt and refuses vtt, and only the timings mean anything to
    it — so the reference is written as bare cues with no words at all."""
    dest.write_text("".join(
        f"{i + 1}\n{_stamp(t)} --> {_stamp(t + 2)}\n-\n\n"
        for i, t in enumerate(cues)))


def align(target_path: str | Path, reference: list[float]) -> Aligned:
    """Re-time `target_path` onto the reference, in place.

    `unchanged` means the alignment was no improvement and the file was left
    exactly as found; `aligned` means it was rewritten; `failed` means there was
    nothing to work with."""
    target = Path(target_path)
    if len(reference) < MIN_REFERENCE_CUES:
        return Aligned("failed", detail="no english track with dialogue in it")
    original = cue_starts(target)
    if len(original) < 20:
        return Aligned("failed", detail="subtitle has almost no cues")

    before, spread = agreement(reference, original)
    if before >= IN_TIME_COVERAGE and spread <= IN_TIME_DISTANCE:
        return Aligned("unchanged", before=before, after=before, distance=spread,
                       detail="already on the reference's clock")

    with tempfile.TemporaryDirectory() as tmp:
        reference_srt = Path(tmp) / "reference.srt"
        _write_srt(reference, reference_srt)
        out = Path(tmp) / "aligned.srt"
        try:
            run = subprocess.run([ALASS, str(reference_srt), str(target), str(out)],
                                 capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            return Aligned("failed", before=before, detail=f"alass: {exc}")
        if run.returncode != 0 or not out.exists() or out.stat().st_size < 200:
            return Aligned("failed", before=before,
                           detail=f"alass returned {run.returncode}")
        after, spread = agreement(reference, cue_starts(out))
        if not after > before:
            return Aligned("unchanged", before=before, after=after, distance=spread)
        target.write_text(out.read_text(encoding="utf-8", errors="replace"))
    log.info("aligned %s: %.0f%% of the english reference now finds a line, was %.0f%%",
             target.name, after * 100, before * 100)
    return Aligned("aligned", before=before, after=after, distance=spread)
