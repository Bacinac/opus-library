import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import mutagen
import mutagen.dsf
import pytest
from mutagen.id3 import TALB, TIT2, TPE1, TPE2, TRCK
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from conftest import run
from opus import db
from opus.models import (Artist, MusicDownload, MusicDownloadStatus, MusicFile, Release,
                         ReleaseStatus, Setting, Track)
from opus.music.metadata import artwork
from opus.music.pipeline import grab, importer, judge, state
from opus.music.tagging import tagger

TITLES = ["Morning", "Noon", "Night"]


def flac(path: Path, title: str, number: int, channels: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-ac", str(channels), "-c:a", "flac",
                    str(path)], check=True)
    audio = mutagen.File(path, easy=True)
    audio["title"], audio["tracknumber"] = title, str(number)
    audio["artist"], audio["album"] = "Somebody", "Record"
    audio.save()
    return path


def dsf(path: Path, title: str, number: int, channels: int = 2,
       sample_rate: int = 2822400) -> Path:
    """A DSF file ffmpeg cannot encode: hand-built to the container's own
    layout (mutagen can read one, and rewrite its ID3 tags, but not create the
    DSD/fmt/data chunks from nothing). What is in the data chunk is never
    decoded by anything this test touches, so it is a handful of zero bytes —
    only the header fields probe_file reads have to be real."""
    path.parent.mkdir(parents=True, exist_ok=True)
    audio_bytes = 64
    dsd_chunk = struct.pack("<4sQQQ", b"DSD ", 28, 0, 0)
    fmt_chunk = struct.pack("<4sQIIIIIIQII", b"fmt ", 52, 1, 0, 2, channels,
                            sample_rate, 1, audio_bytes * 8 // channels, 4096, 0)
    data_chunk = struct.pack("<4sQ", b"data", 12 + audio_bytes)
    path.write_bytes(dsd_chunk + fmt_chunk + data_chunk + bytes(audio_bytes))
    audio = mutagen.dsf.DSF(path)
    audio.add_tags()
    audio.tags.add(TPE1(encoding=3, text=["Somebody"]))
    audio.tags.add(TPE2(encoding=3, text=["Somebody"]))
    audio.tags.add(TALB(encoding=3, text=["Record"]))
    audio.tags.add(TIT2(encoding=3, text=[title]))
    audio.tags.add(TRCK(encoding=3, text=[str(number)]))
    audio.save()
    return path


class Import:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.music = tmp_path / "music"
        self.landing = tmp_path / "landing"
        self.music.mkdir()
        self.said: list[tuple] = []
        self.edition = None
        monkeypatch.setattr(artwork, "refresh_release_artwork", self._nothing)
        monkeypatch.setattr(artwork, "chosen_url", self._no_url)
        monkeypatch.setattr(importer, "_cleanup_downloader", self._cleanup)
        monkeypatch.setattr(grab, "spawn_grab", self._grab)
        monkeypatch.setattr(judge, "_confirm_downloaded_edition", self._edition)
        monkeypatch.setattr(judge, "_looks_transcoded", lambda path: path.name.startswith("fake"))

    async def _nothing(self, *args):
        pass

    async def _no_url(self, *args):
        return None

    async def _cleanup(self, download_id, channel, job_ref, config):
        self.said.append(("cleanup", download_id, Path(job_ref["directory"]).name))

    def _grab(self, release_id, mode="full"):
        self.said.append(("grab", release_id, mode))

    async def _edition(self, release_id, artist_name, album_title, plan, config):
        self.said.append(("edition", release_id, plan.matched))
        if callable(self.edition):
            return await self.edition(release_id)
        return self.edition

    async def setup(self, mode: str = "full", titles=TITLES, settings: dict | None = None,
                    job: str = "job-1") -> tuple[int, int, list[int]]:
        async with db.SessionLocal() as session:
            values = {"music_dir": str(self.music), "opus_landing_dir": str(self.landing),
                      **(settings or {})}
            for key, value in values.items():
                await session.execute(insert(Setting).values(key=key, value=value))
            artist = Artist(name="Somebody")
            session.add(artist)
            await session.flush()
            release = Release(artist_id=artist.id, title="Record", release_date="2001-05-01",
                              status=ReleaseStatus.DOWNLOADING)
            session.add(release)
            await session.flush()
            tracks = [Track(release_id=release.id, position=n + 1, title=title)
                      for n, title in enumerate(titles)]
            session.add_all(tracks)
            download = MusicDownload(release_id=release.id, channel="sabnzbd",
                                     status=MusicDownloadStatus.DOWNLOADING,
                                     job_ref={"directory": f"/landing/{job}", "mode": mode})
            session.add(download)
            await session.commit()
            state.live[download.id] = {"files": []}
            return download.id, release.id, [t.id for t in tracks]

    def job(self, name: str = "job-1") -> Path:
        return self.landing / name

    async def outcome(self, download_id: int, release_id: int) -> dict:
        async with db.SessionLocal() as session:
            download = await session.get(MusicDownload, download_id)
            release = await session.get(Release, release_id)
            files = (await session.execute(
                select(MusicFile.path, Track.title, MusicFile.channels)
                .join(Track, Track.id == MusicFile.track_id)
                .where(Track.release_id == release_id))).all()
            tracks = (await session.execute(
                select(Track.position, Track.title).where(Track.release_id == release_id)
                .order_by(Track.position))).all()
            return {
                "download": (download.status, download.error) if download else None,
                "release": (release.status, release.track_count),
                "files": sorted((str(Path(p).relative_to(self.music)), t, c) for p, t, c in files),
                "tracks": [tuple(t) for t in tracks],
                "live": download_id in state.live,
            }


@pytest.fixture
def importing(tmp_path, monkeypatch, clean):
    return Import(tmp_path, monkeypatch)


@pytest.mark.parametrize("failure", ["late_tag", "commit"])
def test_a_failed_album_replacement_preserves_the_whole_old_album_and_input(importing, monkeypatch, failure):
    from sqlalchemy.ext.asyncio import AsyncSession

    download_id, release_id, _ = run(importing.setup())
    for n, title in enumerate(TITLES, 1):
        flac(importing.job() / f"{n:02d} {title}.flac", title, n)
    run(importer._import_locked(download_id))
    before = {p: p.read_bytes() for p in importing.music.rglob("*.flac")}
    inputs = {p: p.read_bytes() for p in importing.job().rglob("*.flac")}
    catalogue = run(importing.outcome(download_id, release_id))["files"]
    if failure == "late_tag":
        original = tagger._write_tags
        count = 0

        def write(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("second track cannot be tagged")
            return original(*args)

        monkeypatch.setattr(tagger, "_write_tags", write)
    else:
        original = AsyncSession.commit

        async def commit(session):
            if "file_import" in session.info:
                raise OSError("catalogue commit failed")
            await original(session)

        monkeypatch.setattr(AsyncSession, "commit", commit)
    run(importer.import_download(download_id))
    assert {p: p.read_bytes() for p in before} == before
    assert {p: p.read_bytes() for p in inputs} == inputs
    outcome = run(importing.outcome(download_id, release_id))
    assert outcome["files"] == catalogue
    assert outcome["download"][0] is MusicDownloadStatus.FAILED


def test_a_whole_album_is_filed(importing):
    download_id, release_id, _ = run(importing.setup())
    for n, title in enumerate(TITLES, 1):
        flac(importing.job() / f"{n:02d} {title}.flac", title, n)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (MusicDownloadStatus.COMPLETE, None)
    assert said["release"] == (ReleaseStatus.COMPLETE, None)
    assert said["files"] == [
        ("Somebody/Record (2001)/01 - Morning.flac", "Morning", 2),
        ("Somebody/Record (2001)/02 - Noon.flac", "Noon", 2),
        ("Somebody/Record (2001)/03 - Night.flac", "Night", 2),
    ]
    assert not said["live"]
    assert importing.said == [("cleanup", download_id, "job-1")]
    assert mutagen.File(importing.music / said["files"][0][0], easy=True)["tracknumber"] == ["1"]


def test_one_album_is_one_mix(importing):
    download_id, release_id, _ = run(importing.setup())
    flac(importing.job() / "01.flac", "Morning", 1)
    flac(importing.job() / "02.flac", "Noon", 2, channels=1)
    flac(importing.job() / "03.flac", "Night", 3)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (MusicDownloadStatus.REJECTED,
                                "one album is one mix: this download is mono and stereo")
    assert said["release"] == (ReleaseStatus.FAILED, None)
    assert said["files"] == []
    assert importing.said == [("grab", release_id, "full")]


def test_a_surround_edition_must_be_surround(importing):
    download_id, release_id, _ = run(importing.setup(mode="surround"))
    for n, title in enumerate(TITLES, 1):
        flac(importing.job() / f"{n:02d}.flac", title, n)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (
        MusicDownloadStatus.REJECTED,
        "one album is one mix: this download is stereo, and a surround edition was asked for")


def test_a_dsd_edition_must_be_dsd(importing):
    download_id, release_id, _ = run(importing.setup(mode="dsd"))
    for n, title in enumerate(TITLES, 1):
        flac(importing.job() / f"{n:02d}.flac", title, n)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (
        MusicDownloadStatus.REJECTED,
        "one album is one mix: this download is flac, and a DSD edition was asked for")


def test_a_dsd_edition_must_be_stereo(importing):
    download_id, release_id, _ = run(importing.setup(mode="dsd"))
    for n, title in enumerate(TITLES, 1):
        dsf(importing.job() / f"{n:02d}.dsf", title, n, channels=6)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (
        MusicDownloadStatus.REJECTED,
        "one album is one mix: this download is 5.1, and a DSD edition — "
        "stereo only — was asked for")


def test_a_dsd_import_stands_beside_the_flac_and_not_over_it(importing):
    download_id, release_id, track_ids = run(importing.setup(mode="dsd"))
    held = flac(importing.music / "Somebody/Record (2001)/01 - Morning.flac", "Morning", 1)

    async def hold():
        async with db.SessionLocal() as session:
            session.add(MusicFile(path=str(held), track_id=track_ids[0], channels=2, codec="flac"))
            await session.commit()

    run(hold())
    for n, title in enumerate(TITLES, 1):
        dsf(importing.job() / f"{n:02d}.dsf", title, n)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert held.exists()
    assert [f[0] for f in said["files"]] == [
        "Somebody/Record (2001)/01 - Morning.flac",
        "Somebody/Record (2001)/dsd/01 - Morning.dsf",
        "Somebody/Record (2001)/dsd/02 - Noon.dsf",
        "Somebody/Record (2001)/dsd/03 - Night.dsf",
    ]
    assert said["release"] == (ReleaseStatus.COMPLETE, None)


def test_a_flac_import_does_not_delete_the_dsd_edition(importing):
    download_id, release_id, track_ids = run(importing.setup())
    held = dsf(importing.music / "Somebody/Record (2001)/dsd/01 - Morning.dsf", "Morning", 1)

    async def hold():
        async with db.SessionLocal() as session:
            session.add(MusicFile(path=str(held), track_id=track_ids[0], channels=2, codec="dsf"))
            await session.commit()

    run(hold())
    for n, title in enumerate(TITLES, 1):
        flac(importing.job() / f"{n:02d}.flac", title, n)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert held.exists()
    assert [f[0] for f in said["files"]] == [
        "Somebody/Record (2001)/01 - Morning.flac",
        "Somebody/Record (2001)/02 - Noon.flac",
        "Somebody/Record (2001)/03 - Night.flac",
        "Somebody/Record (2001)/dsd/01 - Morning.dsf",
    ]


def test_a_replacement_must_be_whole(importing):
    download_id, release_id, _ = run(importing.setup(mode="replace"))
    flac(importing.job() / "01.flac", "Morning", 1)
    flac(importing.job() / "02.flac", "Noon", 2)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (MusicDownloadStatus.REJECTED,
                                "would leave 1 of 3 tracks unfilled — not a replacement")
    assert importing.said == [("grab", release_id, "full")]


def test_a_lossless_profile_refuses_a_transcode(importing):
    download_id, release_id, _ = run(importing.setup(
        settings={"music_quality_profile": "lossless_only"}))
    flac(importing.job() / "01.flac", "Morning", 1)
    flac(importing.job() / "fake02.flac", "Noon", 2)
    flac(importing.job() / "03.flac", "Night", 3)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (
        MusicDownloadStatus.REJECTED,
        "lossless_only profile, but FLAC transcoded from a lossy source: fake02.flac")


def test_a_short_download_is_filed_and_the_rest_is_hunted(importing):
    download_id, release_id, _ = run(importing.setup())
    flac(importing.job() / "01.flac", "Morning", 1)
    flac(importing.job() / "03.flac", "Night", 3)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (MusicDownloadStatus.COMPLETE, None)
    assert said["release"] == (ReleaseStatus.NONE, None)
    assert [f[1] for f in said["files"]] == ["Morning", "Night"]
    assert importing.said == [("edition", release_id, 2), ("cleanup", download_id, "job-1"),
                              ("grab", release_id, "replace")]


def test_a_different_pressing_becomes_the_record(importing):
    download_id, release_id, _ = run(importing.setup())
    pressing = ["Dawn", "Morning", "Dusk", "Night"]
    for n, title in enumerate(pressing, 1):
        flac(importing.job() / f"{n:02d}.flac", title, n)
    importing.edition = SimpleNamespace(titles=pressing, evidence="discogs release 9")
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["release"] == (ReleaseStatus.COMPLETE, 4)
    assert said["tracks"] == [(1, "Dawn"), (2, "Morning"), (3, "Dusk"), (4, "Night")]
    assert [f[0] for f in said["files"]] == [
        "Somebody/Record (2001)/01 - Dawn.flac", "Somebody/Record (2001)/02 - Morning.flac",
        "Somebody/Record (2001)/03 - Dusk.flac", "Somebody/Record (2001)/04 - Night.flac"]
    assert importing.said[0] == ("edition", release_id, 2)


def test_a_subset_pressing_keeps_only_its_tracks(importing):
    download_id, release_id, _ = run(importing.setup())
    flac(importing.job() / "01.flac", "Morning", 1)
    flac(importing.job() / "02.flac", "Night", 2)
    importing.edition = SimpleNamespace(titles=[], evidence="count")
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["release"] == (ReleaseStatus.COMPLETE, 2)
    assert said["tracks"] == [(1, "Morning"), (3, "Night")]
    assert ("grab", release_id, "replace") not in importing.said


def test_a_fresh_file_supersedes_its_own_edition_only(importing):
    download_id, release_id, track_ids = run(importing.setup())
    old_stereo = flac(importing.music / "old" / "01 - Morning.mp3.flac", "Morning", 1)
    old_surround = flac(importing.music / "old" / "5.1" / "01 - Morning.flac", "Morning", 1, channels=6)

    async def hold():
        async with db.SessionLocal() as session:
            session.add_all([
                MusicFile(path=str(old_stereo), track_id=track_ids[0], channels=2),
                MusicFile(path=str(old_surround), track_id=track_ids[0], channels=6)])
            await session.commit()

    run(hold())
    for n, title in enumerate(TITLES, 1):
        flac(importing.job() / f"{n:02d}.flac", title, n)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert not old_stereo.exists() and old_surround.exists()
    assert [f[0] for f in said["files"]] == [
        "Somebody/Record (2001)/01 - Morning.flac", "Somebody/Record (2001)/02 - Noon.flac",
        "Somebody/Record (2001)/03 - Night.flac", "old/5.1/01 - Morning.flac"]
    assert said["release"] == (ReleaseStatus.COMPLETE, None)


def test_a_cancelled_download_is_not_written_over(importing):
    download_id, release_id, _ = run(importing.setup())
    flac(importing.job() / "01.flac", "Morning", 1)

    async def cancel(release_id):
        async with db.SessionLocal() as session:
            download = await session.get(MusicDownload, download_id)
            download.status = MusicDownloadStatus.CANCELLED
            await session.commit()

    importing.edition = cancel
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (MusicDownloadStatus.CANCELLED, None)
    assert said["files"] == [] and (importing.job() / "01.flac").exists()


def test_a_folder_outside_the_landing_zone_fails(importing):
    download_id, release_id, _ = run(importing.setup(job="../elsewhere"))

    async def elsewhere():
        async with db.SessionLocal() as session:
            download = await session.get(MusicDownload, download_id)
            download.job_ref = {"directory": "/somewhere/else"}
            await session.commit()

    run(elsewhere())
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"][0] == MusicDownloadStatus.FAILED
    assert "is not inside its landing root" in said["download"][1]
    assert said["release"] == (ReleaseStatus.FAILED, None)
    assert not said["live"]


def test_a_download_that_is_gone_is_left_alone(importing):
    run(importer._import_locked(987654))
    assert importing.said == []


def test_a_different_mix_than_the_shelf_holds_is_refused(importing):
    download_id, release_id, track_ids = run(importing.setup())
    held = flac(importing.music / "held" / "01.flac", "Morning", 1)

    async def hold():
        async with db.SessionLocal() as session:
            session.add(MusicFile(path=str(held), track_id=track_ids[0], channels=2))
            await session.commit()

    run(hold())
    for n, title in enumerate(TITLES, 1):
        flac(importing.job() / f"{n:02d}.flac", title, n, channels=6)
    run(importer._import_locked(download_id))
    said = run(importing.outcome(download_id, release_id))
    assert said["download"] == (MusicDownloadStatus.REJECTED,
                                "one album is one mix: this download is 5.1 but the release holds stereo")
    assert said["release"] == (ReleaseStatus.NONE, None)
    assert importing.said == [("grab", release_id, "replace")]


def test_the_cover_and_the_portrait_go_with_the_files(importing, monkeypatch):
    download_id, release_id, _ = run(importing.setup())
    for n, title in enumerate(TITLES, 1):
        flac(importing.job() / f"{n:02d}.flac", title, n)

    async def chosen(session, kind, entity_id):
        return f"https://images.example/{kind}.jpg"

    async def fetch(url):
        return url.encode(), "image/jpeg"

    monkeypatch.setattr(artwork, "chosen_url", chosen)
    monkeypatch.setattr(artwork, "fetch_image", fetch)
    run(importer._import_locked(download_id))
    album = importing.music / "Somebody" / "Record (2001)"
    assert (album / "cover.jpg").read_bytes() == b"https://images.example/release.jpg"
    assert (importing.music / "Somebody" / "folder.jpg").read_bytes() == b"https://images.example/artist.jpg"
    assert [p.data for p in mutagen.File(album / "01 - Morning.flac").pictures] == [
        b"https://images.example/release.jpg"]
