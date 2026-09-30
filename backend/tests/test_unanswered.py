import logging

from opus.music.tagging.tagger import probe_file
from opus.video.subtitles.probe import detect_srt_lang


def test_an_unreadable_audio_file_is_empty_and_said(tmp_path, caplog):
    junk = tmp_path / "01 Balkan.flac"
    junk.write_bytes(b"fLaC" + b"\x00" * 64)
    with caplog.at_level(logging.WARNING, logger="opus.music.tagging"):
        assert probe_file(junk) == {}
    assert "unreadable audio file" in caplog.text and "01 Balkan.flac" in caplog.text


def test_a_subtitle_says_its_own_language(tmp_path):
    srt = tmp_path / "film.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\n"
                   "Where were you last night? I waited for you until the morning came.\n\n"
                   "2\n00:00:04,000 --> 00:00:06,000\nI was at home, reading the letters you sent.\n")
    assert detect_srt_lang(srt) == "en"
