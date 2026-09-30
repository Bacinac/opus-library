"""What a history kept elsewhere needs to know a song or an episode by: the
MusicBrainz ids the file was tagged with, and the series an episode is of as
TMDB knows it."""
from conftest import library, run, signed_in
from opus import db
from opus.models import Artist, Episode, MusicFile, Release, ReleaseStatus, Season, Series, Track

RECORDING = "c6a52bb6-e76e-410b-87d7-e3ba42942d38"
RELEASE = "b2ef5558-d72d-45a9-af7d-6be4e49d52cd"
ARTISTS = ["481bf5f9-2e7c-4c44-b08a-05b32bc7c00d", "39801ecc-1f82-4907-80ac-917350ed188e"]


def test_a_track_and_an_episode_carry_the_ids_another_catalogue_knows_them_by(clean):
    async def build():
        async with db.SessionLocal() as session:
            session.add(Artist(id=1, name="Haustor"))
            await session.flush()
            session.add(Release(id=11, artist_id=1, title="Treći Svijet", status=ReleaseStatus.COMPLETE))
            await session.flush()
            session.add_all([Track(id=101, release_id=11, position=1, title="Radio"),
                             Track(id=102, release_id=11, position=2, title="Take")])
            await session.flush()
            session.add_all([
                MusicFile(path="/music/h/01.flac", track_id=101, tags={
                    "MUSICBRAINZ_TRACKID": RECORDING, "MUSICBRAINZ_ALBUMID": RELEASE,
                    "MUSICBRAINZ_ARTISTID": ARTISTS}),
                MusicFile(path="/music/h/02.flac", track_id=102, tags={
                    "MUSICBRAINZ_ARTISTID": ARTISTS[0]}),
            ])
            series = Series(tmdb_id=1399, title="Show", year=2011)
            session.add(series)
            await session.flush()
            season = Season(series_id=series.id, number=2)
            session.add(season)
            await session.flush()
            episode = Episode(season_id=season.id, number=3, title="Three")
            session.add(episode)
            await session.commit()
            return episode.id

    episode = run(build())
    token = run(signed_in("boss"))

    async def ask():
        async with library(token) as client:
            tracks = (await client.get("/api/music/tracks", params={"ids": "101,102"})).json()
            episodes = (await client.get("/api/video/episodes", params={"ids": str(episode)})).json()
            return tracks, episodes

    tracks, episodes = run(ask())
    ids = {t["id"]: t["mbids"] for t in tracks}
    assert ids[101] == {"recording": RECORDING, "release": RELEASE, "artists": ARTISTS}
    assert ids[102] == {"recording": None, "release": None, "artists": [ARTISTS[0]]}
    assert episodes[0]["series_tmdb_id"] == 1399
    assert (episodes[0]["season_number"], episodes[0]["number"]) == (2, 3)
