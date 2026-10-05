from pathlib import Path

import pytest

from opus.video.subtitles import probe


@pytest.mark.parametrize(("tag", "code"), [
    ("eng", "en"), ("en", "en"), ("English", "en"), ("hrv", "hr"), ("scr", "hr"), ("cro", "hr"),
    ("hrvatski", "hr"), ("scc", "sr"), ("srp", "sr"),
    # the three-letter codes a first-two-letters cut turns into other languages
    ("swe", "sv"), ("por", "pt"), ("chi", "zh"), ("zho", "zh"), ("dut", "nl"), ("cze", "cs"),
    ("gre", "el"), ("jpn", "ja"), ("may", "ms"), ("ind", "id"), ("bul", "bg"), ("lav", "lv"),
    ("per", "fa"), ("alb", "sq"),
    ("und", "und"), ("", "und"), (None, "und"), ("xyz", "und"),
])
def test_a_language_tag_becomes_its_two_letter_code(tag, code):
    assert probe.norm_lang(tag) == code


@pytest.fixture
def release(tmp_path):
    video = tmp_path / "Show.S01E05.1080p.mkv"
    video.write_bytes(b"")
    return video


def named(video: Path, name: str, text: str = "") -> Path:
    path = video.parent / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(("name", "lang", "forced"), [
    ("Show.S01E05.1080p.hr.srt", "hr", False),
    ("Show.S01E05.1080p.hr.forced.srt", "hr", True),
    ("Show.S01E05.1080p.en.sdh.srt", "en", False),
    ("Show.S01E05.1080p.2.eng.srt", "en", False),
    ("Show.S01E05.1080p.swe.ass", "sv", False),
    ("Subs/2_English.srt", "en", False),
])
def test_a_sidecar_name_says_its_language_past_the_words_about_the_track(release, name, lang, forced):
    said = probe.sidecar(release, named(release, name))
    assert (said["lang"], said["forced"], said["format"]) == (lang, forced, name.rsplit(".", 1)[1])


def test_a_bare_srt_is_read_for_its_language(release):
    text = "".join(f"{n}\n00:00:{n:02d},000 --> 00:00:{n:02d},900\nThe house was quiet and everyone "
                   f"had gone to sleep before the storm came in from the sea.\n\n" for n in range(1, 30))
    assert probe.sidecar(release, named(release, "Show.S01E05.1080p.srt", text))["lang"] == "en"


def test_a_download_excludes_other_episodes_while_allowing_unambiguous_generic_subtitles(release):
    named(release, "Show.S01E05.1080p.hr.srt")
    named(release, "Subs/3_Croatian.srt")
    named(release, "Show.S01E06.1080p.hr.srt")
    named(release, "Show.S01E05.1080p.hr.4.opus.vtt")
    around = {Path(s["path"]).name for s in probe.sidecar_subs(release)}
    own = {Path(s["path"]).name for s in probe.own_sidecars(release)}
    assert around == {"Show.S01E05.1080p.hr.srt", "3_Croatian.srt"}
    assert own == {"Show.S01E05.1080p.hr.srt"}


def test_a_season_pack_assigns_only_identified_episode_subtitles(release):
    named(release, "Show.S01E06.1080p.mkv")
    for name in ("Show.S01E05.hr.srt", "Other.Release.S01E05.en.srt",
                 "Show.S02E05.hr.srt", "Show.S01E06.hr.srt", "Subs/2_English.srt",
                 "Subs/Show.S01E05/3_Croatian.srt"):
        named(release, name)
    selected = {Path(sub["path"]).relative_to(release.parent).as_posix()
                for sub in probe.sidecar_subs(release)}
    assert selected == {"Show.S01E05.hr.srt", "Other.Release.S01E05.en.srt",
                        "Subs/Show.S01E05/3_Croatian.srt"}


@pytest.mark.parametrize(("rate", "number"), [
    ("24000/1001", 23.976), ("25/1", 25.0), ("0/0", None), ("0/1", None), ("", None), (None, None),
    ("x/2", None),
])
def test_a_frame_rate_fraction_is_a_number_or_nothing(rate, number):
    assert probe._rate(rate) == number
