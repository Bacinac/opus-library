import math

import pytest

from opus.video.subtitles import sync, timing

SRT = """1
00:00:05,000 --> 00:00:07,500
first

3
01:29:58,250 --> 01:30:00,000
the end

2
00:45:00,000 --> 00:45:02,5
middle, written out of order
"""

ASS = """[Events]
Dialogue: 0,0:00:05.00,0:00:07.00,Default,,0,0,0,,first
Dialogue: 0,1:10:00.00,1:10:02.50,Default,,0,0,0,,last
"""


def test_the_last_cue_is_the_furthest_one_not_the_last_written(tmp_path):
    srt = tmp_path / "a.srt"
    srt.write_text(SRT)
    assert timing.last_cue_seconds(srt) == 5400.0
    ass = tmp_path / "a.ass"
    ass.write_text(ASS)
    assert timing.last_cue_seconds(ass) == 4202.5
    assert timing.last_cue_seconds(tmp_path / "missing.srt") is None


@pytest.mark.parametrize(("duration", "verdict"), [
    (5500.0, timing.FITS), (5400 / 1.02, timing.FITS), (5400 / 0.8, timing.FITS),
    (3600.0, timing.MISMATCH), (9000.0, timing.MISMATCH), (None, timing.UNKNOWN), (0, timing.UNKNOWN),
])
def test_a_subtitle_fits_a_film_it_ends_within(tmp_path, duration, verdict):
    srt = tmp_path / "a.srt"
    srt.write_text(SRT)
    fit = timing.check(srt, duration)
    assert fit.verdict == verdict and fit.mismatch == (verdict == timing.MISMATCH)


def test_the_reason_is_readable_in_every_case(tmp_path):
    empty = tmp_path / "empty.srt"
    empty.write_text("nothing timed here")
    assert timing.describe(timing.check(empty, 100.0), 100.0) == "no cue timings in the file"
    srt = tmp_path / "a.srt"
    srt.write_text(SRT)
    assert timing.describe(timing.check(srt, None), None) == "last cue 5400s, running time unknown"
    assert timing.describe(timing.check(srt, 6000.0), 6000.0) == \
        "last cue 5400s against 6000s of picture, ratio 0.90"


def test_cue_starts_come_back_in_order(tmp_path):
    srt = tmp_path / "a.srt"
    srt.write_text(SRT)
    assert sync.cue_starts(srt) == [5.0, 2700.0, 5398.25]


def test_a_displaced_subtitle_loses_coverage_not_just_distance():
    reference = [float(t) for t in range(0, 1000, 10)]
    assert sync.agreement(reference, [t + 0.5 for t in reference]) == (1.0, 0.5)
    coverage, distance = sync.agreement(reference, [t + 40 for t in reference[:5]])
    assert coverage < sync.IN_TIME_COVERAGE
    assert sync.agreement(reference, []) == (0.0, math.inf)


def test_a_written_reference_reads_back_as_the_same_cues(tmp_path):
    cues = [0.0, 61.5, 3723.125]
    dest = tmp_path / "reference.srt"
    sync._write_srt(cues, dest)
    assert sync.cue_starts(dest) == cues
    assert sync._stamp(-3) == "00:00:00,000"
