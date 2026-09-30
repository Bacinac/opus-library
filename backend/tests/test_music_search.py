from conftest import library, run, signed_in
from opus import db
from opus.api.routers.music import library as music_library
from opus.models import Artist, MusicFile, Release, Track


def album(artist, title, released="2020-01-01", album_id=1):
    return {"source": "deezer", "id": album_id, "title": title, "artist": {"id": 9, "name": artist},
            "release_date": released, "cover_medium": f"https://img/{album_id}.jpg"}


class Catalogue:
    albums: list = []

    async def search_albums(self, query):
        return self.albums

    async def close(self):
        pass


async def _shelf():
    async with db.SessionLocal() as session:
        session.add_all([
            Artist(id=1, name="Prljavo Kazalište", monitored=True),
            Artist(id=2, name="Jala Brat", monitored=True),
            Artist(id=3, name="Prljavi Inspektor Blaža", monitored=False),
        ])
        await session.flush()
        session.add(Release(id=11, artist_id=1, title="Crno-bijeli svijet"))
        await session.flush()
        session.add(Track(id=101, release_id=11, position=1, title="Mi plešemo"))
        await session.flush()
        session.add(MusicFile(path="/music/pk/01.flac", track_id=101, codec="flac"))
        await session.commit()


def test_one_search_finds_the_shelf_first_and_then_only_records_not_on_it(clean, monkeypatch):
    Catalogue.albums = [
        album("Prljavo Kazalište", "Crno-bijeli svijet", "1980-01-01", 1),
        album("Prljavo Kazaliste", "Heroj ulice", "1981-05-01", 2),
        album("Prljavo Kazaliste", "Heroj ulice", "1981-05-01", 3),
        album("Jala Brat", "Mafija", "2019-01-01", 4),
    ]
    monkeypatch.setattr(music_library, "CatalogClient", Catalogue)

    async def scenario():
        await _shelf()
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return (await client.get("/api/music/search", params={"q": "prljavo"}),
                    await client.get("/api/music/search", params={"q": "  "}))

    found, blank = run(scenario())
    said = found.json()
    assert said["artists"] == [1]
    assert [(t["title"], t["artist"]) for t in said["tracks"]] == [("Mi Plešemo", "Prljavo Kazalište")]
    assert said["records"] == [{"kind": "album", "source": "deezer", "id": "2", "title": "Heroj ulice",
                                "artist": "Prljavo Kazaliste", "year": 1981,
                                "cover_url": "https://img/2.jpg"}]
    assert blank.json() == {"artists": [], "tracks": [], "records": []}


def test_a_catalogue_that_does_not_answer_still_leaves_the_shelf(clean, monkeypatch):
    class Broken(Catalogue):
        async def search_albums(self, query):
            raise RuntimeError("the catalogue refused")

    monkeypatch.setattr(music_library, "CatalogClient", Broken)

    async def scenario():
        await _shelf()
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return await client.get("/api/music/search", params={"q": "kazaliste"})

    said = run(scenario()).json()
    assert said["artists"] == [1] and said["records"] == []
    assert [t["title"] for t in said["tracks"]] == ["Mi Plešemo"]
