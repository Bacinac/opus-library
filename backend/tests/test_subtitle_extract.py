import asyncio
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import Subtitle, VideoFile
from opus.video import library_scan
from opus.video.subtitles import extract


def _ffmpeg(rc: int, sizes: dict[int, int]):
    """A stand-in ffmpeg: every `-map 0:s:N ... -y <target>` pair gets a file of
    the size given for N, then it exits with `rc`."""
    async def fake(*args, **kwargs):
        maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
        targets = [args[i + 1] for i, a in enumerate(args) if a == "-y"]
        for stream, target in zip(maps, targets):
            Path(target).write_bytes(b"x" * sizes.get(int(stream.rsplit(":", 1)[1]), 0))

        async def communicate():
            return b"", b""
        return SimpleNamespace(communicate=communicate, returncode=rc)
    return fake


def test_a_track_read_through_and_found_empty_is_none_and_a_failed_read_is_absent(tmp_path, monkeypatch):
    video = tmp_path / "film.mkv"
    video.write_bytes(b"x")
    wanted = [{"position": 1, "lang": "en"}, {"position": 2, "lang": "hr"}]
    full = str(extract.vtt_for(video, "hr", 2))

    monkeypatch.setattr(extract, "asyncio", SimpleNamespace(
        create_subprocess_exec=_ffmpeg(0, {1: 7, 2: 100}), subprocess=asyncio.subprocess))
    assert run(extract.extract(video, wanted)) == {1: None, 2: full}
    assert not extract.vtt_for(video, "en", 1).exists()

    monkeypatch.setattr(extract, "asyncio", SimpleNamespace(
        create_subprocess_exec=_ffmpeg(1, {1: 7, 2: 100}), subprocess=asyncio.subprocess))
    assert run(extract.extract(video, wanted)) == {2: full}


def test_a_hollow_track_is_marked_and_not_asked_for_again(tmp_path, clean, monkeypatch):
    video = tmp_path / "Show - S01E01.mkv"
    video.write_bytes(b"x")

    async def seed():
        async with db.SessionLocal() as session:
            media = VideoFile(path=str(video))
            session.add(media)
            await session.flush()
            for position, codec in ((1, "ass"), (2, "subrip")):
                session.add(Subtitle(file_id=media.id, lang="en", source="embedded",
                                     format=codec, stream_index=position))
            await session.commit()
    run(seed())

    asked = []

    async def fake_extract(path, wanted):
        asked.append(sorted(w["position"] for w in wanted))
        written = extract.vtt_for(path, "en", 2)
        written.write_bytes(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n")
        return {1: None, 2: str(written)}
    monkeypatch.setattr(extract, "extract", fake_extract)

    run(library_scan._extract_all())
    run(library_scan._extract_all())

    async def rows():
        async with db.SessionLocal() as session:
            return {s.stream_index: (s.hollow, s.vtt_path)
                    for s in (await session.scalars(select(Subtitle))).all()}
    assert run(rows()) == {1: (True, None), 2: (False, str(extract.vtt_for(video, "en", 2)))}
    assert asked == [[1, 2]]
