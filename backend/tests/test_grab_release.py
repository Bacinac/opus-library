import logging

import pytest
from sqlalchemy import select

from conftest import run
from opus import db
from opus.acquire import AcquireError
from opus.models import (Artist, MusicDownload, MusicDownloadStatus, Release, ReleaseStatus,
                         Track)
from opus.music.pipeline import choose, state
from opus.music.pipeline.grab import MAX_RECOVERY_DOWNLOADS, _leave_partial, grab_release
from opus.music.channels.base import Candidate, CandidateFile, Contents
from opus.music.metadata import tracklists

TITLES = ["Morning", "Noon", "Night"]


def post(channel: str, title: str, tracks=TITLES) -> Candidate:
    return Candidate(channel=channel, title=title, ref={"title": title},
                     files=[CandidateFile(name=f"0{n} {t}.flac", size=30_000_000,
                                          extension="flac")
                            for n, t in enumerate(tracks, 1)])


class Channel:
    def __init__(self, heard: list, name: str, posts=(), partial: bool = True,
                 refuses: bool = False, fails: bool = False, then=None, starting=None,
                 undroppable: bool = False):
        self.heard, self.name, self.posts = heard, name, list(posts)
        self.supports_partial, self.refuses, self.fails, self.then = partial, refuses, fails, then
        self.starting, self.undroppable = starting, undroppable

    async def search(self, artist: str, album: str):
        self.heard.append(("search", self.name, artist, album, dict(state.searching)))
        if self.then is not None:
            await self.then()
        if self.refuses:
            raise AcquireError("indexer is down")
        return self.posts

    async def inspect(self, candidate):
        return Contents("unknown")

    async def download(self, candidate, wanted_titles=None):
        self.heard.append(("download", self.name, candidate.title, wanted_titles))
        if self.fails:
            raise AcquireError("transfer refused")
        if self.starting is not None:
            await self.starting()
        return {"job": candidate.title}

    async def drop_job(self, job_ref):
        self.heard.append(("drop", self.name, job_ref))
        if self.undroppable:
            raise AcquireError("OPUS unreachable")


class Grab:
    def __init__(self, monkeypatch):
        self.heard: list[tuple] = []
        self.channels: list[Channel] = []
        self.tracklist = True
        monkeypatch.setattr(state, "searching", {})
        monkeypatch.setattr(choose, "enabled_channels", lambda config: self.channels)
        monkeypatch.setattr(tracklists, "ensure_tracks", self._tracks)

    def channel(self, name: str, *posts, **behaviour) -> Channel:
        made = Channel(self.heard, name, posts, **behaviour)
        self.channels.append(made)
        return made

    async def _tracks(self, session, release):
        self.heard.append(("tracks", release.id))
        if not self.tracklist:
            raise tracklists.DiscographyError("no configured source provides a tracklist")
        rows = [Track(release_id=release.id, position=n, title=t)
                for n, t in enumerate(TITLES, 1)]
        session.add_all(rows)
        await session.flush()
        return rows

    def searched(self) -> list[str]:
        return [h[1] for h in self.heard if h[0] == "search"]


@pytest.fixture
def grab(monkeypatch, clean):
    return Grab(monkeypatch)


async def _release(*downloads: tuple[str, MusicDownloadStatus, str | None]) -> int:
    async with db.SessionLocal() as session:
        artist = Artist(name="Somebody")
        session.add(artist)
        await session.flush()
        release = Release(artist_id=artist.id, title="Record", status=ReleaseStatus.WANTED)
        session.add(release)
        await session.flush()
        for channel, status, title in downloads:
            session.add(MusicDownload(release_id=release.id, channel=channel, status=status,
                                      job_ref={"title": title} if title else None))
        await session.commit()
        return release.id


async def _downloads(release_id: int) -> list[MusicDownload]:
    async with db.SessionLocal() as session:
        return list((await session.execute(
            select(MusicDownload).where(MusicDownload.release_id == release_id)
            .order_by(MusicDownload.id))).scalars())


def _grab(release_id: int, mode: str = "full") -> tuple:
    async def scenario():
        before = {d.id for d in await _downloads(release_id)}
        await grab_release(release_id, mode)
        async with db.SessionLocal() as session:
            release = await session.get(Release, release_id)
        return (release.status if release else None), [
            (d.channel, d.status, d.job_ref, round(d.score))
            for d in await _downloads(release_id) if d.id not in before]
    return run(scenario())


def test_a_grab_walks_the_channels_until_one_carries_the_album(grab):
    release_id = run(_release())
    grab.channel("slskd", post("slskd", "user:Record"), refuses=True)
    grab.channel("sabnzbd", post("sabnzbd", "Somebody-Elsewhere", ["Other", "Songs"]))
    grab.channel("sabnzbd", post("sabnzbd", "Somebody-Record"), fails=True)
    grab.channel("slskd", post("slskd", "user2:Record"))

    status, downloads = _grab(release_id)

    assert grab.heard == [
        ("tracks", release_id),
        ("search", "slskd", "Somebody", "Record", {release_id: "slskd"}),
        ("search", "sabnzbd", "Somebody", "Record", {release_id: "sabnzbd"}),
        ("search", "sabnzbd", "Somebody", "Record", {release_id: "sabnzbd"}),
        ("download", "sabnzbd", "Somebody-Record", None),
        ("search", "slskd", "Somebody", "Record", {release_id: "slskd"}),
        ("download", "slskd", "user2:Record", None),
    ]
    assert state.searching == {}
    assert status == ReleaseStatus.DOWNLOADING
    assert downloads == [("slskd", MusicDownloadStatus.DOWNLOADING,
                          {"job": "user2:Record", "mode": "full", "multi_album": False}, 70)]


def test_a_dead_source_is_skipped_but_usenet_only_loses_its_spent_posts(grab):
    release_id = run(_release(("slskd", MusicDownloadStatus.FAILED, None),
                              ("sabnzbd", MusicDownloadStatus.REJECTED, "Somebody-Record-Old"),
                              ("sabnzbd", MusicDownloadStatus.COMPLETE, "Somebody-Record-Good")))
    grab.channel("slskd", post("slskd", "user:Record"))
    grab.channel("sabnzbd", post("sabnzbd", "Somebody-Record-Old"),
                 post("sabnzbd", "Somebody-Record-Good"), partial=False)

    status, downloads = _grab(release_id)

    assert grab.searched() == ["sabnzbd"]
    assert status == ReleaseStatus.DOWNLOADING
    assert [(d[0], d[2]["job"], d[2]["mode"]) for d in downloads] == [
        ("sabnzbd", "Somebody-Record-Good", "full")]


def test_a_recovery_asks_whole_album_channels_first_for_a_post_not_yet_pulled(grab):
    release_id = run(_release(("sabnzbd", MusicDownloadStatus.COMPLETE, "Somebody-Record-Good")))
    grab.channel("slskd", post("slskd", "user:Record"))
    grab.channel("sabnzbd", post("sabnzbd", "Somebody-Record-Good"), partial=False)
    grab.channel("sabnzbd", post("sabnzbd", "Somebody-Record-Good"),
                 post("sabnzbd", "Somebody-Record-Other"), partial=False)

    status, downloads = _grab(release_id, "replace")

    assert grab.searched() == ["sabnzbd", "sabnzbd"]
    assert status == ReleaseStatus.DOWNLOADING
    assert [(d[0], d[2]) for d in downloads] == [
        ("sabnzbd", {"job": "Somebody-Record-Other", "mode": "replace",
                     "multi_album": False})]


def test_a_recovery_stops_at_the_cap_and_a_grab_does_not(grab):
    spent = [("slskd", MusicDownloadStatus.COMPLETE, None)] * MAX_RECOVERY_DOWNLOADS
    capped = run(_release(*spent))
    grab.channel("slskd", post("slskd", "user:Record"))

    assert _grab(capped, "replace") == (ReleaseStatus.NONE, [])
    assert grab.searched() == []

    status, downloads = _grab(capped, "full")
    assert grab.searched() == ["slskd"]
    assert status == ReleaseStatus.DOWNLOADING and len(downloads) == 1


def test_a_surround_grab_takes_only_a_post_that_says_so(grab):
    release_id = run(_release())
    grab.channel("slskd", post("slskd", "user:Record"), post("slskd", "user:Record 5.1"))

    status, downloads = _grab(release_id, "surround")

    assert status == ReleaseStatus.DOWNLOADING
    assert [d[2] for d in downloads] == [
        {"job": "user:Record 5.1", "mode": "surround", "multi_album": False}]


def test_a_dsd_grab_takes_only_a_post_that_says_so(grab):
    release_id = run(_release())
    grab.channel("slskd", post("slskd", "user:Record"), post("slskd", "user:Record DSD64"))

    status, downloads = _grab(release_id, "dsd")

    assert status == ReleaseStatus.DOWNLOADING
    assert [d[2] for d in downloads] == [
        {"job": "user:Record DSD64", "mode": "dsd", "multi_album": False}]


def test_a_dsd_grab_reads_a_bare_sacd_rip_the_same_way(grab):
    # "SACD ISO"/"SACD-R" names the disc's own 1-bit stream and nothing else;
    # a bare "SACD" (a PCM transfer of one) does not
    release_id = run(_release())
    grab.channel("slskd", post("slskd", "user:Record 24bit SACD transfer"),
                 post("slskd", "user:Record SACD-R"))

    status, downloads = _grab(release_id, "dsd")

    assert status == ReleaseStatus.DOWNLOADING
    assert [d[2]["job"] for d in downloads] == ["user:Record SACD-R"]


def test_nothing_anywhere_fails_a_grab_and_leaves_a_recovery_as_it_was(grab):
    failed = run(_release())
    grab.channel("slskd", post("slskd", "user:Other", ["Something", "Else"]))
    grab.channel("sabnzbd", refuses=True)

    assert _grab(failed) == (ReleaseStatus.FAILED, [])
    assert _grab(run(_release()), "replace") == (ReleaseStatus.NONE, [])
    assert _grab(run(_release()), "surround") == (ReleaseStatus.FAILED, [])


def test_a_release_without_a_tracklist_fails_before_any_search(grab):
    release_id = run(_release())
    grab.tracklist = False
    grab.channel("slskd", post("slskd", "user:Record"))

    assert _grab(release_id) == (ReleaseStatus.FAILED, [])
    assert grab.heard == [("tracks", release_id)]


def test_a_release_that_vanishes_is_no_failure(grab, caplog):
    gone = run(_release())

    async def delete():
        async with db.SessionLocal() as session:
            await session.delete(await session.get(Release, gone))
            await session.commit()

    grab.channel("slskd", post("slskd", "user:Other", ["Something", "Else"]), then=delete)
    caplog.set_level(logging.INFO, logger="opus.music.pipeline")

    assert _grab(gone) == (None, [])
    assert _grab(987654) == (None, [])
    assert grab.searched() == ["slskd"]
    said = [(r.levelname, r.getMessage()) for r in caplog.records
            if r.name == "opus.music.pipeline"]
    assert ("INFO", f"release {gone}: deleted while it was being searched") in said
    assert ("ERROR", "grab_release: release 987654 not found") in said
    assert not any("failed" in message for _, message in said)


def _deleter(release_id: int):
    async def delete():
        async with db.SessionLocal() as session:
            await session.delete(await session.get(Release, release_id))
            await session.commit()
    return delete


def _said(caplog) -> list[tuple[str, str, bool]]:
    return [(r.levelname, r.getMessage(), bool(r.exc_info)) for r in caplog.records
            if r.name == "opus.music.pipeline"]


def test_a_release_deleted_during_its_search_starts_no_download(grab, caplog):
    gone = run(_release())
    grab.channel("slskd", post("slskd", "user:Record"), then=_deleter(gone))
    caplog.set_level(logging.INFO, logger="opus.music.pipeline")

    assert _grab(gone) == (None, [])
    assert [h[0] for h in grab.heard] == ["tracks", "search"]
    said = _said(caplog)
    assert ("INFO", f"release {gone}: deleted while it was being searched", False) in said
    assert not any(level == "ERROR" or trace for level, _, trace in said)


def test_a_release_deleted_as_its_download_starts_has_the_job_dropped(grab, caplog):
    gone = run(_release())
    grab.channel("slskd", post("slskd", "user:Record"), starting=_deleter(gone))
    grab.channel("sabnzbd", post("sabnzbd", "Somebody-Record"), partial=False)
    caplog.set_level(logging.INFO, logger="opus.music.pipeline")

    assert _grab(gone) == (None, [])
    assert grab.heard[1:] == [
        ("search", "slskd", "Somebody", "Record", {gone: "slskd"}),
        ("download", "slskd", "user:Record", None),
        ("drop", "slskd", {"job": "user:Record"}),
    ]
    said = _said(caplog)
    assert ("INFO", f"release {gone}: deleted as its download started, "
                    "job user:Record dropped", False) in said
    assert not any(level == "ERROR" or trace for level, _, trace in said)


def test_a_job_that_cannot_be_dropped_is_named(grab, caplog):
    gone = run(_release())
    grab.channel("slskd", post("slskd", "user:Record"), starting=_deleter(gone),
                 undroppable=True)
    caplog.set_level(logging.INFO, logger="opus.music.pipeline")

    assert _grab(gone) == (None, [])
    assert [h[0] for h in grab.heard] == ["tracks", "search", "download", "drop"]
    said = _said(caplog)
    assert ("ERROR", f"release {gone}: deleted as its download started, and job "
                     "user:Record could not be dropped: OPUS unreachable", False) in said
    assert not any(trace for _, _, trace in said)


def test_a_recovery_cap_on_a_vanished_release_is_no_failure(clean, caplog):
    caplog.set_level(logging.INFO, logger="opus.music.pipeline")

    run(_leave_partial(987654))

    assert _said(caplog) == [
        ("INFO", "release 987654: deleted while it was being searched", False)]
