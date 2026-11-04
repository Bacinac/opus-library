from conftest import library, run, signed_in
from opus import db
from opus.models import Artist, Episode, MusicFile, Release, Season, Series, Track, VideoFile


async def _series() -> dict:
    async with db.SessionLocal() as session:
        session.add_all([Series(id=1, tmdb_id=11, title="Bitange i princeze"),
                         Series(id=2, tmdb_id=12, title="Smogovci")])
        session.add_all([Season(id=1, series_id=1, number=1), Season(id=2, series_id=2, number=1)])
        session.add_all([Episode(id=1, season_id=1, number=1, runtime_min=30),
                         Episode(id=2, season_id=1, number=2, runtime_min=30),
                         Episode(id=3, season_id=1, number=3, runtime_min=25),
                         Episode(id=4, season_id=1, number=4, runtime_min=30),
                         Episode(id=5, season_id=2, number=1, runtime_min=45)])
        session.add_all([VideoFile(path="/v/1.mkv", episode_id=1, duration_s=1500.0),
                         VideoFile(path="/v/2a.mkv", episode_id=2, duration_s=1560.0),
                         VideoFile(path="/v/2b.mkv", episode_id=2, duration_s=1620.0),
                         VideoFile(path="/v/3.mkv", episode_id=3)])
        await session.commit()
    async with library(await signed_in("boss")) as client:
        got = await client.get("/api/video/series")
    assert got.status_code == 200
    return {s["title"]: s for s in got.json()}


def test_a_series_is_timed_by_the_episodes_on_disk(clean):
    shelf = run(_series())
    held = shelf["Bitange i princeze"]
    # 25 min probed, the longer of two copies (27), and TMDB's 25 where the
    # file was never probed; the fourth episode is not here
    assert (held["episodes_have"], held["minutes_have"]) == (3, 77)
    assert (shelf["Smogovci"]["episodes_have"], shelf["Smogovci"]["minutes_have"]) == (0, 0)


async def _music() -> dict:
    async with db.SessionLocal() as session:
        session.add(Artist(id=1, name="Azra", monitored=True))
        session.add(Release(id=1, artist_id=1, title="Sunčana strana ulice"))
        session.add_all([Track(id=1, release_id=1, position=1, title="Užas je moja furka"),
                         Track(id=2, release_id=1, position=2, title="Kurvini sinovi")])
        session.add_all([MusicFile(path="/m/1.flac", track_id=1, duration_sec=3600),
                         MusicFile(path="/m/1-5.1.flac", track_id=1, duration_sec=3600),
                         MusicFile(path="/m/2.flac", track_id=2, duration_sec=5400),
                         MusicFile(path="/m/loose.flac", duration_sec=36000)])
        await session.commit()
    async with library(await signed_in("boss")) as client:
        got = await client.get("/api/music/library/stats")
    assert got.status_code == 200
    return got.json()


def test_the_music_shelf_is_timed_once_per_track(clean):
    stats = run(_music())
    # a track held twice is heard once, and a file the catalogue could not
    # place is not on the shelf
    assert (stats["tracks"], stats["hours"]) == (4, 2)
