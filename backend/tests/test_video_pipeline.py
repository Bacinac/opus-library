import asyncio
import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from conftest import library, run, signed_in
from opus import acquire, db
from opus.acquire import AcquireError, JobStatus
from opus.models import Episode, Movie, Season, Series, Setting, Subtitle, VideoDownload, VideoFile
from opus.settings_store import current_runtime
from opus.video.metadata import tmdb
from opus.video.pipeline import download, grab, importer, monitor, search
from opus.video.channels.base import Candidate
from opus.video.subtitles import fetcher


class Stop(BaseException):
    pass


MINUTE = datetime.timedelta(minutes=1)
HOUR = datetime.timedelta(hours=1)
DAY = datetime.timedelta(days=1)


def ago(delta: datetime.timedelta) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - delta


async def _film(tmdb_id: int = 1) -> int:
    async with db.SessionLocal() as session:
        movie = Movie(tmdb_id=tmdb_id, title="Film", year=2020)
        session.add(movie)
        await session.commit()
        return movie.id


async def _download(movie_id: int, **fields) -> int:
    async with db.SessionLocal() as session:
        row = VideoDownload(kind="movie", movie_id=movie_id, channel="sabnzbd",
                            **{"release_title": "Film.2020.1080p", "state": "queued",
                               "job_ref": {"job": "j1"}, **fields})
        session.add(row)
        await session.flush()
        if row.state == "failed":
            await grab.bury(session, row)
        await session.commit()
        return row.id


async def _row(download_id: int) -> VideoDownload:
    async with db.SessionLocal() as session:
        return await session.get(VideoDownload, download_id)


async def _setting(key: str, value: str) -> None:
    async with db.SessionLocal() as session:
        await session.execute(insert(Setting).values(key=key, value=value))
        await session.commit()


@pytest.fixture
def poll(clean, monkeypatch, caplog):
    async def stop(seconds):
        raise Stop

    monkeypatch.setattr(download, "asyncio", SimpleNamespace(sleep=stop, to_thread=asyncio.to_thread))
    monkeypatch.setattr(download, "_channel_poll_counter", 0)

    statuses: dict[str, JobStatus | Exception] = {}
    dropped: list[dict] = []
    replaced: list[int] = []

    async def job_status(config, job_ref):
        answer = statuses[job_ref["job"]]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def drop(config, job_ref):
        dropped.append(job_ref)

    async def try_next_release(session, config, dl):
        replaced.append(dl.id)

    monkeypatch.setattr(acquire, "job_status", job_status)
    monkeypatch.setattr(acquire, "drop", drop)
    monkeypatch.setattr(grab, "try_next_release", try_next_release)

    def once():
        with pytest.raises(Stop):
            run(download.poll_downloads_loop())
        assert "download poll loop iteration failed" not in caplog.text

    return SimpleNamespace(statuses=statuses, dropped=dropped, replaced=replaced, once=once)


def test_progress_moves_a_queued_job_and_a_started_one_becomes_downloading(poll):
    film = run(_film())
    waiting = run(_download(film, job_ref={"job": "j1"}))
    started = run(_download(film, job_ref={"job": "j2"}))
    poll.statuses.update(j1=JobStatus("queued", progress=0.0, detail="in line"),
                         j2=JobStatus("downloading", progress=0.42, detail="12 MB/s"))
    poll.once()

    first, second = run(_row(waiting)), run(_row(started))
    assert (first.state, first.progress, first.detail) == ("queued", 0.0, "in line")
    assert (second.state, second.progress, second.detail) == ("downloading", 0.42, "12 MB/s")
    assert poll.replaced == [] and poll.dropped == []


def test_a_job_opus_cannot_account_for_is_kept_waiting_inside_the_grace(poll):
    film = run(_film())
    fresh = run(_download(film, state="downloading", progress=0.3,
                          created_at=ago(acquire.UNACCOUNTED_GRACE - MINUTE)))
    poll.statuses["j1"] = JobStatus("unknown", detail="OPUS has no such job")
    poll.once()

    row = run(_row(fresh))
    assert (row.state, row.detail) == ("downloading", "OPUS has no such job")
    assert poll.replaced == []


def test_a_job_forgotten_past_the_grace_fails_and_the_next_release_is_asked_for(poll):
    film = run(_film())
    lost = run(_download(film, created_at=ago(acquire.UNACCOUNTED_GRACE + MINUTE)))
    poll.statuses["j1"] = JobStatus("unknown", detail="OPUS has no such job")
    poll.once()

    row = run(_row(lost))
    assert (row.state, row.detail) == ("failed", "sabnzbd lost the job: OPUS has no such job")
    assert poll.replaced == [lost]


def test_a_failed_job_fails_its_row_with_the_reason(poll):
    film = run(_film())
    failing = run(_download(film, state="downloading"))
    poll.statuses["j1"] = JobStatus("failed", progress=0.7, detail="missing articles")
    poll.once()

    row = run(_row(failing))
    assert (row.state, row.progress, row.detail) == ("failed", 0.7, "missing articles")
    assert poll.replaced == [failing]


def test_opus_unreachable_leaves_the_row_as_it_was_and_says_why(poll):
    film = run(_film())
    polled = run(_download(film, state="downloading", progress=0.5))
    poll.statuses["j1"] = AcquireError("OPUS status failed: connection refused")
    poll.once()

    row = run(_row(polled))
    assert (row.state, row.progress, row.detail) == (
        "downloading", 0.5, "OPUS status failed: connection refused")
    assert poll.replaced == []


def test_a_complete_job_records_its_folder_before_the_import_and_drops_the_job(poll, monkeypatch):
    seen = []

    async def import_download(session, config, dl, directory):
        async with db.SessionLocal() as other:
            stored = await other.get(VideoDownload, dl.id)
            seen.append((stored.state, stored.job_ref["landing"], directory))
        dl.state = "imported"

    monkeypatch.setattr(importer, "import_download", import_download)
    film = run(_film())
    done = run(_download(film, state="downloading"))
    poll.statuses["j1"] = JobStatus("complete", progress=1.0, directory="/landing/movies/Film")
    poll.once()

    assert seen == [("downloaded", "/landing/movies/Film", "/landing/movies/Film")]
    assert run(_row(done)).state == "imported"
    assert poll.dropped == [{"job": "j1", "landing": "/landing/movies/Film"}]
    assert poll.replaced == []


def test_the_job_is_kept_when_cleanup_after_import_is_off(poll, monkeypatch):
    async def import_download(session, config, dl, directory):
        dl.state = "imported"

    monkeypatch.setattr(importer, "import_download", import_download)
    run(_setting("cleanup_after_import", "false"))
    film = run(_film())
    run(_download(film))
    poll.statuses["j1"] = JobStatus("complete", directory="/landing/movies/Film")
    poll.once()

    assert poll.dropped == []


def test_a_failed_import_fails_the_row_and_asks_for_the_next_release(poll, monkeypatch):
    async def import_download(session, config, dl, directory):
        dl.state = "imported"
        raise importer.ImportFailure(f"completed download not found at {directory}")

    monkeypatch.setattr(importer, "import_download", import_download)
    film = run(_film())
    broken = run(_download(film))
    poll.statuses["j1"] = JobStatus("complete", directory="/landing/movies/Film")
    poll.once()

    row = run(_row(broken))
    assert (row.state, row.detail) == ("failed", "completed download not found at /landing/movies/Film")
    assert row.job_ref["landing"] == "/landing/movies/Film"
    assert poll.replaced == [broken] and poll.dropped == []


def test_an_import_stranded_by_a_restart_is_resumed_from_the_recorded_folder(poll, monkeypatch):
    seen = []

    async def import_download(session, config, dl, directory):
        seen.append(directory)
        dl.state = "imported"

    monkeypatch.setattr(importer, "import_download", import_download)
    film = run(_film())
    stranded = run(_download(film, state="downloaded",
                             job_ref={"job": "j1", "landing": "/landing/movies/Film"}))
    poll.once()

    assert seen == ["/landing/movies/Film"]
    assert run(_row(stranded)).state == "imported"


async def _waiting(film: int, **fields) -> int:
    async with db.SessionLocal() as session:
        media = VideoFile(path="/movies/Film (2020)/Film (2020).mkv", movie_id=film)
        session.add(media)
        await session.flush()
        row = VideoDownload(kind="movie", movie_id=film, channel="sabnzbd",
                            release_title="Film.2020.1080p", state="waiting_subtitles",
                            progress=1.0, detail="en,hr", updated_at=ago(HOUR),
                            job_ref={"job": "j1", "file_id": media.id}, **fields)
        session.add(row)
        await session.commit()
        return row.id


def test_a_file_waiting_for_subtitles_is_asked_again_only_after_the_retry_interval(poll, monkeypatch):
    asked = []

    async def acquire_missing(session, config, media, langs, *, query_hint=""):
        asked.append(tuple(langs))
        return []

    monkeypatch.setattr(fetcher, "acquire_missing", acquire_missing)
    waiting = run(_waiting(run(_film())))
    poll.once()
    poll.once()

    row = run(_row(waiting))
    assert asked == [("en", "hr")]
    assert (row.state, row.detail) == ("waiting_subtitles", "en,hr")
    assert row.updated_at > ago(MINUTE)


def test_subtitles_that_arrive_on_a_retry_complete_the_import(poll, monkeypatch):
    async def acquire_missing(session, config, media, langs, *, query_hint=""):
        session.add(Subtitle(file_id=media.id, lang="hr", source="opensubtitles",
                             format="srt", path="/movies/Film (2020)/Film (2020).hr.srt"))
        await session.flush()
        return [{"lang": "hr"}]

    monkeypatch.setattr(fetcher, "acquire_missing", acquire_missing)
    waiting = run(_waiting(run(_film())))
    poll.once()

    row = run(_row(waiting))
    assert (row.state, row.progress, row.detail) == ("imported", 1.0, "")


def test_a_wait_whose_file_row_is_gone_fails(poll):
    film = run(_film())

    async def orphan():
        async with db.SessionLocal() as session:
            row = VideoDownload(kind="movie", movie_id=film, channel="sabnzbd",
                                state="waiting_subtitles", updated_at=ago(HOUR),
                                job_ref={"job": "j1", "file_id": 999})
            session.add(row)
            await session.commit()
            return row.id

    gone = run(orphan())
    poll.once()

    row = run(_row(gone))
    assert (row.state, row.detail) == ("failed", "imported file row is gone")


@pytest.fixture
def releases(clean, monkeypatch):
    offered: list[Candidate] = []
    grabbed: list[tuple[dict, str]] = []

    async def search_candidates(config, **item):
        return offered

    async def acquired(config, grab_ref, namespace):
        grabbed.append((grab_ref, namespace))
        return f"job-{len(grabbed) + 1}"

    monkeypatch.setattr(search, "search_candidates", search_candidates)
    monkeypatch.setattr(acquire, "grab", acquired)
    return SimpleNamespace(offered=offered, grabbed=grabbed)


def release(title: str, guid: str = "") -> Candidate:
    return Candidate(channel="sabnzbd", title=title, size=1, protocol="usenet",
                     guid=guid or f"guid-{title}", ref={"grab_ref": {"title": title}})


def replace(download_id: int):
    async def go():
        async with db.SessionLocal() as session:
            config = await current_runtime()
            dl = await session.get(VideoDownload, download_id)
            await grab.try_next_release(session, config, dl)
        async with db.SessionLocal() as session:
            return (await session.execute(
                select(VideoDownload.release_title, VideoDownload.state, VideoDownload.job_ref)
                .where(VideoDownload.id != download_id).order_by(VideoDownload.id))).all()

    return run(go())


def test_a_failed_post_is_never_taken_again_but_a_re_post_of_its_title_is(releases):
    film = run(_film())
    run(_download(film, release_title="Film.B", release_guid="post-b", state="failed",
                  created_at=ago(30 * DAY)))
    failed = run(_download(film, release_title="Film.A", release_guid="post-a1", state="failed"))
    releases.offered.extend([release("Film.A", "post-a1"), release("Film.B", "post-b"),
                             release("Film.A", "post-a2")])

    rows = replace(failed)
    assert releases.grabbed == [({"title": "Film.A"}, "movies")]
    assert [tuple(r) for r in rows] == [
        ("Film.B", "failed", {"job": "j1"}),
        ("Film.A", "queued", {"job": "job-2", "category": "movies", "title": "Film.A"})]


def test_a_release_already_on_its_way_is_not_doubled(releases):
    film = run(_film())
    run(_download(film, release_title="Film.B", state="downloading"))
    failed = run(_download(film, release_title="Film.A", state="failed"))
    releases.offered.append(release("Film.C"))

    replace(failed)
    assert releases.grabbed == []


def test_the_walk_goes_on_past_any_number_of_dead_posts(releases):
    film = run(_film())
    for n in range(9):
        run(_download(film, release_title=f"Film.{n}", release_guid=f"guid-Film.{n}", state="failed"))
    failed = run(_download(film, release_title="Film.last", release_guid="guid-Film.last",
                           state="failed"))
    releases.offered.extend([release(f"Film.{n}") for n in range(9)] + [release("Film.fresh")])

    replace(failed)
    assert [ref["title"] for ref, _ in releases.grabbed] == ["Film.fresh"]


def test_nothing_is_grabbed_once_every_release_has_been_tried(releases):
    film = run(_film())
    failed = run(_download(film, release_title="Film.A", release_guid="guid-Film.A", state="failed"))
    releases.offered.append(release("Film.A"))

    assert replace(failed) == []
    assert releases.grabbed == []


def test_sidecars_that_would_share_a_name_are_all_kept(clean, tmp_path, monkeypatch):
    release = tmp_path / "landing" / "Film.2020.1080p"
    (release / "Subs").mkdir(parents=True)
    source = release / "Film.2020.1080p.mkv"
    source.write_bytes(b"picture")
    sidecars = {
        "Subs/2_English.srt": b"first by its own name",
        "Subs/3_English.srt": b"second by its own name",
        "Film.2020.1080p.en.srt": b"english",
        "Film.2020.1080p.eng.srt": b"english again",
    }
    for name, content in sidecars.items():
        (release / name).write_bytes(content)
    dest = tmp_path / "movies" / "Film (2020)" / "Film (2020).mkv"

    async def probe(path):
        return {"container": "matroska", "video_codec": "h264", "width": 1920, "height": 1080,
                "audio_langs": ["en"], "duration_s": 6000.0, "subtitle_streams": []}

    monkeypatch.setattr(importer, "probe_file", probe)

    async def scenario():
        movie_id = await _film()
        download_id = await _download(movie_id)
        async with db.SessionLocal() as session:
            config = await current_runtime()
            dl = await session.get(VideoDownload, download_id)
            media = await importer._persist_file(session, dl, config, source, dest)
            await session.commit()
            return (await session.execute(
                select(Subtitle.lang, Subtitle.path).where(Subtitle.file_id == media.id))).all()

    rows = run(scenario())
    paths = [path for _, path in rows]
    assert len(set(paths)) == 4
    assert sorted(Path(path).read_bytes() for path in paths) == sorted(sidecars.values())
    assert sorted(lang for lang, _ in rows) == ["en", "en", "en", "en"]
    assert all(path.startswith(str(dest.parent / "Film (2020).")) for path in paths)


def test_sidecars_already_named_for_their_video_stay_where_they_are(tmp_path):
    dest = tmp_path / "Film (2020).mkv"
    kept = [{"path": str(tmp_path / "Film (2020).en.srt"), "lang": "en", "format": "srt"},
            {"path": str(tmp_path / "Film (2020).2.en.srt"), "lang": "en", "format": "srt"},
            {"path": str(tmp_path / "Other.en.srt"), "lang": "en", "format": "srt"}]
    assert importer._sidecar_names(dest, kept) == [
        tmp_path / "Film (2020).en.srt", tmp_path / "Film (2020).2.en.srt",
        tmp_path / "Film (2020).3.en.srt"]


def test_a_replacement_retires_the_old_file_and_keeps_its_own_subtitles(clean, tmp_path, monkeypatch):
    shelf = tmp_path / "movies" / "Film (2020)"
    shelf.mkdir(parents=True)
    old_video = shelf / "Film (2020).mp4"
    old_video.write_bytes(b"bad sound")
    old_sub = shelf / "Film (2020).en.srt"
    old_sub.write_bytes(b"old english")

    release = tmp_path / "landing" / "Film.2020.2160p"
    release.mkdir(parents=True)
    source = release / "Film.2020.2160p.mkv"
    source.write_bytes(b"good sound")
    (release / "Film.2020.2160p.en.srt").write_bytes(b"new english")
    dest = shelf / "Film (2020).mkv"

    async def probe(path):
        return {"container": "matroska", "video_codec": "hevc", "width": 3840, "height": 2160,
                "audio_langs": ["en"], "duration_s": 6000.0, "subtitle_streams": []}

    monkeypatch.setattr(importer, "probe_file", probe)

    async def scenario():
        movie_id = await _film()
        async with db.SessionLocal() as session:
            old = VideoFile(path=str(old_video), movie_id=movie_id, release_guid="guid-old")
            session.add(old)
            await session.flush()
            session.add(Subtitle(file_id=old.id, lang="en", source="external", format="srt",
                                 path=str(old_sub)))
            await session.commit()
        download_id = await _download(movie_id, release_guid="guid-new")
        async with db.SessionLocal() as session:
            config = await current_runtime()
            dl = await session.get(VideoDownload, download_id)
            media = await importer._persist_file(session, dl, config, source, dest)
            await importer._retire_replaced(session, media)
            await session.commit()
            files = (await session.execute(
                select(VideoFile.path).where(VideoFile.movie_id == movie_id))).scalars().all()
            return files, await grab.held_posts(session, movie_id=movie_id)

    files, held = run(scenario())
    assert files == [str(dest)]
    assert held == {"guid-new"}
    assert not old_video.exists()
    assert dest.read_bytes() == b"good sound"
    assert old_sub.read_bytes() == b"new english"


def test_a_copy_fetched_again_under_the_same_name_leaves_no_old_subtitles(clean, tmp_path, monkeypatch):
    shelf = tmp_path / "movies" / "Film (2020)"
    shelf.mkdir(parents=True)
    dest = shelf / "Film (2020).mkv"
    dest.write_bytes(b"bad sound")
    old_vtt = shelf / "Film (2020).en.3.opus.vtt"
    old_vtt.write_bytes(b"old")
    source = tmp_path / "landing" / "Film.2020.1080p.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"good sound")

    async def probe(path):
        return {"container": "matroska", "video_codec": "h264", "width": 1920, "height": 1080,
                "audio_langs": ["en"], "duration_s": 6000.0, "subtitle_streams": []}

    monkeypatch.setattr(importer, "probe_file", probe)

    async def scenario():
        movie_id = await _film()
        async with db.SessionLocal() as session:
            old = VideoFile(path=str(dest), movie_id=movie_id)
            session.add(old)
            await session.flush()
            session.add(Subtitle(file_id=old.id, lang="en", source="embedded", format="subrip",
                                 stream_index=3, vtt_path=str(old_vtt)))
            await session.commit()
        download_id = await _download(movie_id)
        async with db.SessionLocal() as session:
            config = await current_runtime()
            dl = await session.get(VideoDownload, download_id)
            await importer._persist_file(session, dl, config, source, dest)
            await session.commit()

    run(scenario())
    assert dest.read_bytes() == b"good sound"
    assert not old_vtt.exists()


def test_the_release_list_says_what_became_of_each_post_already_taken(quick, monkeypatch):
    dead, going, fresh = (release("Film.2020.1080p-DEAD"), release("Film.2020.1080p-GOING"),
                          release("Film.2020.1080p-FRESH"))

    async def search_candidates(config, *, movie=None, episode=None, session=None):
        return [dead, going, fresh]

    monkeypatch.setattr(search, "search_candidates", search_candidates)

    async def scenario():
        movie_id = await _film()
        await _download(movie_id, release_title=dead.title, release_guid=dead.guid, state="failed")
        await _download(movie_id, release_title=going.title, release_guid=going.guid,
                        state="downloading")
        cookie = await signed_in("filip")
        async with library(cookie) as client:
            return await client.get(f"/api/video/movies/{movie_id}/releases")

    listed = run(scenario())
    assert listed.status_code == 200
    assert [(r["title"], r["standing"]) for r in listed.json()] == [
        (dead.title, "failed"), (going.title, "coming"), (fresh.title, "")]


def test_the_monitor_takes_an_episode_whose_air_date_just_arrived(releases, monkeypatch):
    aired = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    releases.offered.append(release("Show.S01E01.1080p"))

    async def said(config, path, **params):
        if path.endswith("/season/1"):
            return {"episodes": [{"id": 11, "episode_number": 1, "name": "Pilot",
                                  "air_date": aired, "overview": "It begins."}]}
        return {"id": 1, "name": "Show", "seasons": [{"season_number": 1}]}

    monkeypatch.setattr(tmdb, "_get", said)

    async def scenario():
        async with db.SessionLocal() as session:
            series = Series(tmdb_id=1, title="Show", monitored=True)
            session.add(series)
            await session.flush()
            season = Season(series_id=series.id, number=1, monitored=True)
            session.add(season)
            await session.flush()
            session.add(Episode(season_id=season.id, number=1, title="Episode 1", monitored=True))
            await session.commit()
        async with db.SessionLocal() as session:
            await monitor._monitor_pass(session, await current_runtime())
        async with db.SessionLocal() as session:
            return (await session.execute(select(VideoDownload.release_title))).scalars().all()

    assert run(scenario()) == ["Show.S01E01.1080p"]


def test_clearing_a_failed_row_from_the_queue_leaves_its_post_dead(quick, monkeypatch):
    dead = release("Film.2020.1080p-DEAD")

    async def search_candidates(config, *, movie=None, episode=None, session=None):
        return [dead]

    monkeypatch.setattr(search, "search_candidates", search_candidates)

    async def scenario():
        movie_id = await _film()
        row = await _download(movie_id, release_title=dead.title, release_guid=dead.guid,
                              state="failed")
        cookie = await signed_in("filip")
        async with library(cookie) as client:
            cleared = await client.delete(f"/api/video/downloads/{row}")
            listed = await client.get(f"/api/video/movies/{movie_id}/releases")
        return cleared, listed

    cleared, listed = run(scenario())
    assert cleared.status_code == 204
    assert [r["standing"] for r in listed.json()] == ["failed"]
