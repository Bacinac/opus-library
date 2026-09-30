import datetime

from sqlalchemy.dialects.postgresql import insert

from conftest import library, run, signed_in
from opus import db
from opus.models import (Artist, ArtistExternalId, ArtistRelation, Image, MusicDownload,
                         MusicDownloadStatus, MusicFile, Release, ReleaseStatus,
                         ReleaseVariant, Setting, Track)
from opus.music.pipeline import state

SEEN = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)


async def _shelf() -> None:
    async with db.SessionLocal() as session:
        await session.execute(insert(Setting).values(key="release_filter", value="albums"))
        band = Artist(id=1, name="Riblja Čorba", deezer_id=100, wikidata_id="Q1",
                      artist_type="group", country="Serbia", country_hr="Srbija", begin_year=1978,
                      bio="A rock band.", bio_url="https://sr.wikipedia.org/wiki/Riblja",
                      image_url="https://img/band.jpg", enrich_status="resolved",
                      wiki_studio_albums=["Kost U Grlu"])
        singer = Artist(id=2, name="Bora Đorđević", monitored=False)
        supergroup = Artist(id=3, name="Zvezde", monitored=True)
        other = Artist(id=4, name="Somebody Else")
        session.add_all([band, singer, supergroup, other])
        await session.flush()
        session.add_all([
            ArtistRelation(artist_id=2, related_artist_id=1, relation="member_of"),
            ArtistRelation(artist_id=1, related_artist_id=3, relation="member_of"),
            ArtistRelation(artist_id=4, related_artist_id=1, relation="influenced_by"),
            ArtistExternalId(artist_id=1, source="spotify", external_id="sp1"),
            ArtistExternalId(artist_id=1, source="discogs", external_id="42"),
            ArtistExternalId(artist_id=1, source="lastfm", external_id="riblja"),
            Image(entity_type="artist", entity_id=1, source="discogs",
                  url="https://img/narrow.jpg", width=1500, height=1000),
            Image(entity_type="artist", entity_id=1, source="wikimedia",
                  url="https://img/wide.jpg", width=2000, height=1000),
            Image(entity_type="artist", entity_id=1, source="deezer",
                  url="https://img/portrait.jpg", width=3000, height=3000),
        ])
        kost = Release(id=11, artist_id=1, title="Kost U Grlu", release_date="1979-05-01",
                       deezer_id=1001, wikidata_id="Q11", cover_url="https://img/kost.jpg",
                       status=ReleaseStatus.COMPLETE)
        remaster = Release(id=12, artist_id=1, title="Kost U Grlu (Remastered)",
                           release_date="2009-01-01", discogs_id=5001)
        hidden = Release(id=13, artist_id=1, title="Pokvarena Mašta", record_type="single",
                         release_date="1981-01-01")
        wanted = Release(id=14, artist_id=1, title="Ne Veruj", record_type="single",
                         release_date="1990-02-02", status=ReleaseStatus.WANTED)
        live = Release(id=15, artist_id=1, title="U Ime Naroda", record_type="live",
                       spotify_id="sp15", track_count=12)
        foreign = Release(id=16, artist_id=4, title="Other", release_date="2000")
        session.add_all([kost, remaster, hidden, wanted, live, foreign])
        await session.flush()
        one = Track(id=101, release_id=11, position=1, title="Egoista")
        two = Track(id=102, release_id=11, position=2, title="Mirno Spavaj")
        three = Track(id=103, release_id=11, position=3, title="Kost U Grlu")
        elsewhere = Track(id=104, release_id=16, position=1, title="Other")
        session.add_all([one, two, three, elsewhere])
        await session.flush()
        session.add_all([
            MusicFile(path="/music/Riblja/Kost U Grlu/01.flac", track_id=101, codec="flac",
                      sample_rate_hz=44100, bit_depth=16, bitrate_kbps=900, channels=2,
                      download_id=7),
            MusicFile(path="/music/Riblja/Kost U Grlu/02.flac", track_id=102, codec="flac",
                      sample_rate_hz=96000, bit_depth=24, bitrate_kbps=2000, channels=2,
                      download_id=8),
            MusicFile(path="/music/Riblja/Kost U Grlu/03 (5.1).flac", track_id=103,
                      codec="flac", sample_rate_hz=48000, bit_depth=24, channels=6),
            MusicFile(path="/music/Riblja/Kost U Grlu/dsd/01.dsf", track_id=101, codec="dsf",
                      sample_rate_hz=2822400, bit_depth=1, channels=2),
            MusicFile(path="/music/Riblja/Kost U Grlu/scan.flac"),
            MusicFile(path="/music/Riblja/Kost U Grlu/booklet/extra.flac"),
            MusicFile(path="/music/Riblja/Kost U Grlu 2/stray.flac"),
            MusicFile(path="/music/Other/Other/01.flac", track_id=104, codec="mp3"),
            ReleaseVariant(release_id=11, variant="multichannel", source="discogs",
                           external_id="d11", seen_at=SEEN),
            ReleaseVariant(release_id=11, variant="atmos", source="deezer",
                           external_id="t11", seen_at=SEEN),
        ])
        queued = MusicDownload(id=31, release_id=14, channel="slskd",
                               status=MusicDownloadStatus.QUEUED)
        importing = MusicDownload(id=32, release_id=14, channel="sabnzbd",
                                  status=MusicDownloadStatus.IMPORTING)
        finished = MusicDownload(id=33, release_id=11, channel="sabnzbd",
                                 status=MusicDownloadStatus.COMPLETE)
        session.add_all([queued, importing, finished])
        await session.commit()


EXPECTED_RELEASES = [
    {"id": 12, "title": "Kost U Grlu (Remastered)", "release_date": "2009-01-01",
     "category": "studio", "cover_url": None, "status": "none",
     "searching_channel": None, "progress": None, "quality": None, "files_linked": 0,
     "editions": [], "unmatched_files": 0, "mixed_sources": False, "variants": [],
     "canonical": True, "stands": False, "record_title": "Kost U Grlu",
     "record_date": "1979-05-01", "track_count": None, "sources": ["discogs"]},
    {"id": 14, "title": "Ne Veruj", "release_date": "1990-02-02", "category": "single",
     "cover_url": None, "status": "wanted", "searching_channel": "slskd",
     "progress": 0.75, "quality": None, "files_linked": 0, "editions": [],
     "unmatched_files": 0, "mixed_sources": False, "variants": [], "canonical": False,
     "stands": True, "record_title": "Ne Veruj", "record_date": "1990-02-02",
     "track_count": None, "sources": []},
    {"id": 11, "title": "Kost U Grlu", "release_date": "1979-05-01", "category": "studio",
     "cover_url": "https://img/kost.jpg", "status": "complete",
     "searching_channel": None, "progress": None,
     "quality": {"codec": "flac", "bitrate_kbps": 900, "sample_rate_hz": 44100,
                 "bit_depth": 16, "channels": 2, "mixed": True},
     "files_linked": 3, "editions": ["5.1", "dsd", "stereo"], "unmatched_files": 2,
     "mixed_sources": True,
     "variants": [
         {"variant": "atmos", "source": "deezer", "external_id": "t11",
          "seen_at": "2026-03-01T12:00:00+00:00"},
         {"variant": "multichannel", "source": "discogs", "external_id": "d11",
          "seen_at": "2026-03-01T12:00:00+00:00"},
     ],
     "canonical": True, "stands": True, "record_title": "Kost U Grlu",
     "record_date": "1979-05-01", "track_count": 3,
     "sources": ["deezer", "wikidata"]},
    {"id": 15, "title": "U Ime Naroda", "release_date": None, "category": "live",
     "cover_url": None, "status": "none", "searching_channel": None, "progress": None,
     "quality": None, "files_linked": 0, "editions": [], "unmatched_files": 0,
     "mixed_sources": False, "variants": [], "canonical": False, "stands": True,
     "record_title": "U Ime Naroda", "record_date": None, "track_count": 12,
     "sources": ["spotify"]},
]


def test_an_artist_page_carries_everything_the_shelf_and_the_player_read(quick, monkeypatch):
    monkeypatch.setattr(state, "searching", {14: "slskd"})
    monkeypatch.setattr(state, "live", {31: {"progress": 0.5}, 32: {"progress": 0.75}})

    async def scenario():
        await _shelf()
        async with library(await signed_in("boss")) as client:
            return await client.get("/api/music/artists/1")

    answer = run(scenario())
    assert answer.status_code == 200
    page = answer.json()
    assert list(page) == ["id", "name", "image_url", "backdrop_url", "monitored", "deezer_id",
                          "wikidata_id", "artist_type", "country", "country_hr", "begin_year", "end_year",
                          "bio", "enrich_status", "links", "members", "groups", "releases"]
    assert {k: v for k, v in page.items() if k != "releases"} == {
        "id": 1, "name": "Riblja Čorba", "image_url": "https://img/band.jpg",
        "backdrop_url": "https://img/wide.jpg", "monitored": True, "deezer_id": 100,
        "wikidata_id": "Q1", "artist_type": "group", "country": "Serbia", "country_hr": "Srbija",
        "begin_year": 1978, "end_year": None, "bio": "A rock band.",
        "enrich_status": "resolved",
        "links": [
            {"source": "deezer", "url": "https://www.deezer.com/artist/100"},
            {"source": "spotify", "url": "https://open.spotify.com/artist/sp1"},
            {"source": "discogs", "url": "https://www.discogs.com/artist/42"},
            {"source": "wikidata", "url": "https://www.wikidata.org/wiki/Q1"},
            {"source": "wikipedia", "url": "https://sr.wikipedia.org/wiki/Riblja"},
        ],
        "members": [{"id": 2, "name": "Bora Đorđević", "monitored": False}],
        "groups": [{"id": 3, "name": "Zvezde", "monitored": True}],
    }
    assert [list(r) for r in page["releases"]] == [list(EXPECTED_RELEASES[0])] * 4
    assert page["releases"] == EXPECTED_RELEASES


def test_every_category_shows_when_nothing_is_filtered_and_nothing_vouches(quick):
    async def scenario():
        async with db.SessionLocal() as session:
            session.add(Artist(id=5, name="Garage Band"))
            await session.flush()
            session.add_all([
                Release(id=51, artist_id=5, title="First", release_date="2001"),
                Release(id=52, artist_id=5, title="Second Single", record_type="single",
                        release_date="2002"),
                Release(id=53, artist_id=5, title="Third", record_type="ep",
                        release_date="2003"),
            ])
            await session.execute(insert(Setting).values(key="release_filter", value="all"))
            await session.commit()
        async with library(await signed_in("boss")) as client:
            return (await client.get("/api/music/artists/5"),
                    await client.get("/api/music/artists/999"))

    page, missing = run(scenario())
    body = page.json()
    assert body["links"] == [] and body["members"] == [] and body["groups"] == []
    assert body["backdrop_url"] is None
    assert [(r["id"], r["category"], r["canonical"]) for r in body["releases"]] == [
        (53, "ep", False), (52, "single", False), (51, "studio", True)]
    assert missing.status_code == 404
    assert missing.json()["detail"] == "artist not found"


def test_an_artists_songs_come_once_each_from_the_record_they_first_came_out_on(quick):
    async def scenario():
        await _shelf()
        async with db.SessionLocal() as session:
            session.add(Release(id=17, artist_id=1, title="Najbolje", record_type="compile",
                                release_date="1995-01-01"))
            await session.flush()
            session.add_all([Track(id=171, release_id=17, position=1, title="Egoista (Live)"),
                             Track(id=172, release_id=17, position=2, title="Lutka")])
            await session.flush()
            session.add_all([MusicFile(path="/music/Riblja/Najbolje/01.flac", track_id=171),
                             MusicFile(path="/music/Riblja/Najbolje/02.flac", track_id=172)])
            await session.commit()
        async with library(await signed_in("boss")) as client:
            return (await client.get("/api/music/artists/1/tracks"),
                    await client.get("/api/music/artists/1/tracks", params={"prefer": "dsd"}),
                    await client.get("/api/music/artists/99/tracks"))

    stereo, dsd, nobody = run(scenario())
    assert [(t["id"], t["album"], t["path"].rsplit("/", 1)[1]) for t in stereo.json()] == [
        (101, "Kost U Grlu", "01.flac"), (102, "Kost U Grlu", "02.flac"),
        (103, "Kost U Grlu", "03 (5.1).flac"), (172, "Najbolje", "02.flac")]
    assert dsd.json()[0]["codec"] == "dsf"
    assert nobody.status_code == 404
