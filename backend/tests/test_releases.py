from sqlalchemy import func, select

from conftest import library, run, signed_in
from opus import db
from opus.models import Artist, MusicDownload, MusicFile, Release, Track
from opus.music.pipeline import choose, grab
from opus.music.channels.base import Candidate, CandidateFile
from opus.music.metadata import tracklists


def test_reading_a_release_asks_nobody_and_writes_nothing(quick, monkeypatch):
    asked = []

    async def tracks(session, release):
        asked.append(("tracks", release.id))
        session.add(Track(release_id=release.id, position=1, title="Fetched"))
        await session.flush()

    async def description(release, client=None):
        asked.append(("description", release.id))
        release.description = "From the article"

    async def facts(release, deezer=None):
        asked.append(("facts", release.id))
        release.label = "Label"

    monkeypatch.setattr(tracklists, "ensure_tracks", tracks)
    monkeypatch.setattr(tracklists, "ensure_description", description)
    monkeypatch.setattr(tracklists, "ensure_facts", facts)

    async def scenario():
        async with db.SessionLocal() as session:
            artist = Artist(name="Somebody")
            session.add(artist)
            await session.flush()
            release = Release(artist_id=artist.id, title="Record", deezer_id=5)
            session.add(release)
            await session.commit()
            release_id = release.id
        guest = await signed_in("gost", role="guest")
        admin = await signed_in("boss")
        async with library(guest) as client:
            read = await client.get(f"/api/music/releases/{release_id}/tracks")
            about = await client.get(f"/api/music/releases/{release_id}/about")
            refused = await client.post(f"/api/music/releases/{release_id}/tracks")
        before = list(asked)
        async with library(admin) as client:
            filled = await client.post(f"/api/music/releases/{release_id}/tracks")
        async with db.SessionLocal() as session:
            held = await session.scalar(select(func.count()).select_from(Track))
        return read, about, refused, before, filled, held

    read, about, refused, before, filled, held = run(scenario())
    assert read.status_code == 200 and read.json()["tracks"] == []
    assert read.json()["description"] is None
    assert about.status_code == 200 and about.json()["description"] == ""
    assert refused.status_code == 403
    assert before == []
    assert filled.status_code == 200
    assert [t["title"] for t in filled.json()["tracks"]] == ["Fetched"]
    assert filled.json()["description"] == "From the article"
    assert held == 1


def test_a_release_nobody_knows_is_said_to_be_unknown(quick, monkeypatch):
    async def nothing(session, release):
        raise tracklists.DiscographyError("no configured source provides a tracklist")

    monkeypatch.setattr(tracklists, "ensure_tracks", nothing)

    async def scenario():
        async with db.SessionLocal() as session:
            artist = Artist(name="Somebody")
            session.add(artist)
            await session.flush()
            release = Release(artist_id=artist.id, title="Record")
            session.add(release)
            await session.commit()
            release_id = release.id
        async with library(await signed_in("boss")) as client:
            return await client.post(f"/api/music/releases/{release_id}/tracks")

    answer = run(scenario())
    assert answer.status_code == 409
    assert "no configured source" in answer.json()["detail"]


def test_a_dsd_edition_is_offered_beside_the_stereo_one(quick):
    async def scenario():
        async with db.SessionLocal() as session:
            artist = Artist(name="Somebody")
            session.add(artist)
            await session.flush()
            release = Release(artist_id=artist.id, title="Record")
            session.add(release)
            await session.flush()
            track = Track(release_id=release.id, position=1, title="Morning")
            session.add(track)
            await session.flush()
            session.add_all([
                MusicFile(path="/music/Record/01 - Morning.flac", track_id=track.id,
                          channels=2, codec="flac", sample_rate_hz=44100, bit_depth=16),
                MusicFile(path="/music/Record/dsd/01 - Morning.dsf", track_id=track.id,
                          channels=2, codec="dsf", sample_rate_hz=2822400, bit_depth=1),
            ])
            await session.commit()
            release_id = release.id
        async with library(await signed_in("boss")) as client:
            stereo = await client.get(f"/api/music/releases/{release_id}/playback")
            dsd = await client.get(f"/api/music/releases/{release_id}/playback?prefer=dsd")
        return stereo, dsd

    stereo, dsd = run(scenario())
    assert stereo.status_code == 200 and dsd.status_code == 200
    assert stereo.json()["editions"] == ["dsd", "stereo"]
    assert stereo.json()["tracks"][0]["codec"] == "flac"
    assert dsd.json()["tracks"][0]["codec"] == "dsf"


class _FakeChannel:
    def __init__(self, name: str, *found: Candidate, downloaded: list | None = None):
        self.name, self._found, self._downloaded = name, list(found), downloaded

    async def search(self, artist: str, album: str):
        return self._found

    async def download(self, candidate: Candidate, wanted_titles=None):
        if self._downloaded is not None:
            self._downloaded.append((self.name, candidate.title, candidate.ref))
        return {"job": candidate.title}


async def _release_with_track() -> int:
    async with db.SessionLocal() as session:
        artist = Artist(name="Somebody")
        session.add(artist)
        await session.flush()
        release = Release(artist_id=artist.id, title="Record")
        session.add(release)
        await session.flush()
        session.add(Track(release_id=release.id, position=1, title="Morning"))
        await session.commit()
        return release.id


def test_candidates_lists_what_the_automatic_grab_would_have_refused(quick, monkeypatch):
    # a label whose post names no artist the catalog knows: _title_completeness
    # would score this zero and the automatic path would never take it
    unmatched = Candidate(channel="sabnzbd", title="Stockfisch Records - Closer To The Music",
                          files=[CandidateFile(name="x", size=2_000_000_000, extension=".flac")],
                          ref={"grab_ref": {"engine": "sabnzbd"}, "title": "Stockfisch…"},
                          whole_album=True)
    monkeypatch.setattr(choose, "enabled_channels",
                        lambda config: [_FakeChannel("sabnzbd", unmatched)])

    async def scenario():
        release_id = await _release_with_track()
        async with library(await signed_in("boss")) as client:
            return await client.get(f"/api/music/releases/{release_id}/candidates")

    answer = run(scenario())
    assert answer.status_code == 200
    found = answer.json()["candidates"]
    assert len(found) == 1
    assert found[0]["title"] == "Stockfisch Records - Closer To The Music"
    assert found[0]["completeness"] == 0
    assert found[0]["quality"]["codec"] == "flac"


def test_grab_downloads_exactly_the_chosen_candidate(quick, monkeypatch):
    downloaded: list = []
    monkeypatch.setattr(grab, "enabled_channels",
                        lambda config: [_FakeChannel("sabnzbd", downloaded=downloaded)])
    ref = {"grab_ref": {"engine": "sabnzbd", "nzb_url": "https://x/1"}, "title": "Picked"}

    async def scenario():
        release_id = await _release_with_track()
        async with library(await signed_in("boss")) as client:
            grabbed = await client.post(f"/api/music/releases/{release_id}/grab", json={
                "channel": "sabnzbd", "title": "Picked", "ref": ref, "mode": "dsd"})
        async with db.SessionLocal() as session:
            download = (await session.execute(select(MusicDownload))).scalar_one()
        return grabbed, download

    grabbed, download = run(scenario())
    assert grabbed.status_code == 202
    assert downloaded == [("sabnzbd", "Picked", ref)]
    assert download.channel == "sabnzbd" and download.job_ref["mode"] == "dsd"
    assert download.job_ref["job"] == "Picked"


def test_a_shelf_of_records_is_what_can_be_played_newest_arrival_first(quick):
    import datetime

    day = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)

    async def scenario():
        async with db.SessionLocal() as session:
            session.add(Artist(id=1, name="Haustor"))
            session.add_all([Release(id=1, artist_id=1, title="Treći Svijet", release_date="1984"),
                             Release(id=2, artist_id=1, title="Bolero", release_date="1985",
                                     track_count=9),
                             Release(id=3, artist_id=1, title="Wanted Only")])
            session.add_all([Track(id=11, release_id=1, position=1, title="A"),
                             Track(id=21, release_id=2, position=1, title="B"),
                             Track(id=22, release_id=2, position=2, title="C"),
                             Track(id=31, release_id=3, position=1, title="D")])
            await session.flush()
            session.add_all([
                MusicFile(path="/m/1.flac", track_id=11, created_at=day),
                MusicFile(path="/m/2.flac", track_id=21,
                          created_at=day + datetime.timedelta(days=3)),
            ])
            await session.commit()
        async with library(await signed_in("boss")) as client:
            return (await client.get("/api/music/releases", params={"recent": 5}),
                    await client.get("/api/music/releases", params={"ids": "3,1,2"}))

    recent, named = run(scenario())
    assert [(c["id"], c["held"], c["track_count"]) for c in recent.json()] == [(2, 1, 9), (1, 1, 1)]
    assert [c["id"] for c in named.json()] == [1, 2]
    assert named.json()[0] == {"id": 1, "title": "Treći Svijet", "artist_id": 1,
                               "artist": "Haustor", "year": "1984", "cover_url": None,
                               "held": 1, "track_count": 1}
