import datetime

import httpx
import pytest
import pyvips
from sqlalchemy import select

from conftest import library, run, signed_in
from opus import db
from opus.models import Artist, Image, MusicFile, Picture, RadioStation, Release, Track
from opus.models.pictures import FAILED, HELD, NONE
from opus.pictures import catalogue, door, store, tiles

DEEZER = "https://cdn-images.dzcdn.net/images/cover/0123456789abcdef0123456789abcdef/250x250-000000-80-0-0.jpg"
BLANK = f"https://cdn-images.dzcdn.net/images/cover/{store.DEEZER_EMPTY}/1000x1000-000000-80-0-0.jpg"


def jpeg(width: int, height: int) -> bytes:
    return pyvips.Image.black(width, height, bands=3).write_to_buffer(".jpg")


@pytest.fixture
def held(monkeypatch, tmp_path, clean):
    monkeypatch.setattr(store, "ROOT", tmp_path / "artwork")
    store.prepare()
    return tmp_path


def answering(monkeypatch, handler):
    monkeypatch.setattr(store, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(store, "_throttles", {})
    monkeypatch.setattr(store, "PACE", {})
    monkeypatch.setattr(store, "DEFAULT_PACE", 0)


async def _picture(source: str) -> int:
    return (await door.ids({source}))[source]


async def _row(picture_id: int) -> Picture:
    async with db.SessionLocal() as session:
        return await session.get(Picture, picture_id)


def test_a_catalogue_address_is_asked_at_the_size_the_original_is_kept():
    assert store.masters(DEEZER) == [DEEZER.replace("/250x250-", "/1000x1000-")]
    assert store.masters("https://image.tmdb.org/t/p/w342/abc.jpg") == [
        "https://image.tmdb.org/t/p/w780/abc.jpg"]
    assert store.masters("https://image.tmdb.org/t/p/w300/abc.jpg") == [
        "https://image.tmdb.org/t/p/original/abc.jpg"]
    assert store.masters("https://resources.tidal.com/images/a/b/c/d/e/320x320.jpg") == [
        "https://resources.tidal.com/images/a/b/c/d/e/1280x1280.jpg",
        "https://resources.tidal.com/images/a/b/c/d/e/750x750.jpg"]
    assert store.masters("https://i.discogs.com/signed/rs:fit/w:150/x.jpeg") == []


def test_every_picture_in_an_answer_leaves_as_the_librarys_own_address(held):
    async def scenario():
        answer = {"stations": [{"name": "Yammat", "url": "https://stream.example/live.mp3",
                                "logo": "https://yammat.example/logo.png"}],
                  "release": {"cover_url": DEEZER, "bio_url": "https://en.wikipedia.org/wiki/X"},
                  "cast": [{"name": "Somebody", "profile_url": "https://image.tmdb.org/t/p/w185/p.jpg"}],
                  "poster_url": "/music/Somebody/Record/cover.jpg", "image_url": None}
        await door.ours(answer)
        async with db.SessionLocal() as session:
            ids = dict((await session.execute(select(Picture.source, Picture.id))).all())
        return answer, ids

    answer, ids = run(scenario())
    assert answer["stations"][0] == {"name": "Yammat", "url": "https://stream.example/live.mp3",
                                     "logo": f"/api/pictures/{ids['https://yammat.example/logo.png']}"}
    assert answer["release"] == {"cover_url": f"/api/pictures/{ids[DEEZER]}",
                                 "bio_url": "https://en.wikipedia.org/wiki/X"}
    assert answer["cast"][0]["profile_url"].startswith("/api/pictures/")
    assert answer["poster_url"] == f"/api/pictures/{ids['/music/Somebody/Record/cover.jpg']}"
    assert answer["image_url"] is None
    assert len(ids) == 4


def test_a_picture_the_source_has_not_got_is_handed_out_as_nothing(held, monkeypatch):
    answering(monkeypatch, lambda request: httpx.Response(404))

    async def scenario():
        picture_id = await _picture("https://example.org/gone.jpg")
        await store.hold(picture_id)
        return picture_id, await door.ours({"cover_url": "https://example.org/gone.jpg"})

    picture_id, answer = run(scenario())
    assert run(_row(picture_id)).state == NONE
    assert answer == {"cover_url": None}


def test_deezers_blank_square_is_never_kept(held, monkeypatch):
    def deezer(request):
        return httpx.Response(302, headers={"location": BLANK})

    answering(monkeypatch, deezer)
    monkeypatch.setattr(store, "BLANK_TRIES", 2)
    picture_id = run(_picture(DEEZER))

    run(store.hold(picture_id))
    first = run(_row(picture_id))
    assert (first.state, first.tries, first.file) == (FAILED, 1, None)
    assert first.retry_at > datetime.datetime.now(datetime.UTC)

    async def asked_again():
        async with db.SessionLocal() as session:
            (await session.get(Picture, picture_id)).retry_at = None
            await session.commit()
        await store.hold(picture_id)

    run(asked_again())
    assert run(_row(picture_id)).state == NONE
    assert not list((held / "artwork" / "originals").rglob("*.*"))


def test_a_busy_source_is_asked_again_later_and_its_small_size_is_not_settled_for(held, monkeypatch):
    asked = []

    def busy(request):
        asked.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "600"})

    answering(monkeypatch, busy)
    picture_id = run(_picture(DEEZER))
    run(store.hold(picture_id))
    row = run(_row(picture_id))
    assert row.state == FAILED
    assert row.retry_at > datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=590)
    assert asked == [DEEZER.replace("/250x250-", "/1000x1000-")]


def test_a_picture_is_taken_once_and_drawn_at_the_width_asked(held, monkeypatch):
    asked = []

    def deezer(request):
        asked.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(1000, 1000))

    answering(monkeypatch, deezer)

    async def scenario():
        cookie = await signed_in("ivo")
        picture_id = await _picture(DEEZER)
        async with library(cookie) as client:
            small = await client.get(f"/api/pictures/{picture_id}", params={"w": 300})
            again = await client.get(f"/api/pictures/{picture_id}", params={"w": 300})
            whole = await client.get(f"/api/pictures/{picture_id}", params={"w": 1920})
        return picture_id, small, again, whole

    picture_id, small, again, whole = run(scenario())
    assert small.status_code == again.status_code == whole.status_code == 200
    assert small.headers["content-type"] == "image/jpeg"
    assert pyvips.Image.new_from_buffer(small.content, "").width == 320
    assert pyvips.Image.new_from_buffer(whole.content, "").width == 1000
    assert asked == [DEEZER.replace("/250x250-", "/1000x1000-")]
    row = run(_row(picture_id))
    assert (row.state, row.width, row.kind) == (HELD, 1000, "image/jpeg")


def test_a_larger_size_the_source_refuses_falls_back_to_the_address_given(held, monkeypatch):
    face = "https://resources.tidal.com/images/a/b/c/d/e/320x320.jpg"

    def tidal(request):
        if "/1280x1280" in request.url.path:
            return httpx.Response(403)
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(750, 750))

    answering(monkeypatch, tidal)
    picture_id = run(_picture(face))
    run(store.hold(picture_id))
    assert run(_row(picture_id)).width == 750


def test_an_oversized_original_is_kept_at_the_master_edge(held, monkeypatch):
    def itunes(request):
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg(3000, 3000))

    answering(monkeypatch, itunes)
    picture_id = run(_picture("https://is1-ssl.mzstatic.com/image/thumb/x/3000x3000bb.jpg"))
    run(store.hold(picture_id))
    assert run(_row(picture_id)).width == store.MASTER_EDGE


def test_the_picture_beside_a_record_is_its_original(held, monkeypatch):
    music = held / "music"
    monkeypatch.setattr(store, "LIBRARY_TREES", (f"{music}/",))
    record = music / "Somebody" / "(2001) Record"
    (record / "CD1").mkdir(parents=True)
    (record / "Cover.JPG").write_bytes(jpeg(600, 600))
    (music / "Somebody" / "artist.jpg").write_bytes(jpeg(400, 400))
    picked = music / "Picked" / "(1999) Other"
    picked.mkdir(parents=True)
    (picked / "cover.jpg").write_bytes(jpeg(600, 600))
    (music / "Picked" / "folder.jpg").write_bytes(jpeg(400, 400))

    async def scenario():
        async with db.SessionLocal() as session:
            somebody = Artist(name="Somebody")
            chosen = Artist(name="Picked", image_url="https://example.org/picked-by-hand.jpg")
            session.add_all([somebody, chosen])
            await session.flush()
            record_row = Release(artist_id=somebody.id, title="Record", cover_url=DEEZER)
            other = Release(artist_id=chosen.id, title="Other", cover_url=None)
            session.add_all([record_row, other])
            await session.flush()
            for release, path in ((record_row, record / "CD1" / "01.flac"), (other, picked / "01.flac")):
                track = Track(release_id=release.id, position=1, title="One")
                session.add(track)
                await session.flush()
                session.add(MusicFile(path=str(path), track_id=track.id))
            session.add(Image(entity_type="artist", entity_id=chosen.id, source="deezer",
                              url=chosen.image_url, chosen=True, chosen_manual=True))
            await session.commit()
            ids = (somebody.id, chosen.id, record_row.id, other.id)
        attached = await catalogue.attach()
        async with db.SessionLocal() as session:
            rows = {p.source: p for p in (await session.execute(select(Picture))).scalars()}
            artists = [await session.get(Artist, i) for i in ids[:2]]
            releases = [await session.get(Release, i) for i in ids[2:]]
        return attached, rows, artists, releases

    attached, rows, (somebody, chosen), (record_row, other) = run(scenario())
    assert attached == 3
    assert (rows[DEEZER].state, rows[DEEZER].file) == (HELD, str(record / "Cover.JPG"))
    assert somebody.image_url == str(music / "Somebody" / "artist.jpg")
    assert rows[somebody.image_url].file == somebody.image_url
    assert other.cover_url == str(picked / "cover.jpg")
    assert chosen.image_url == "https://example.org/picked-by-hand.jpg"
    assert chosen.image_url not in rows

    async def drawn():
        cookie = await signed_in("ivo")
        async with library(cookie) as client:
            return await client.get(f"/api/pictures/{rows[DEEZER].id}", params={"w": 160})

    answer = run(drawn())
    assert answer.status_code == 200
    assert pyvips.Image.new_from_buffer(answer.content, "").width == 160


def test_a_file_that_has_gone_from_the_library_is_fetched_again(held, monkeypatch):
    answering(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "image/jpeg"}, content=jpeg(1000, 1000)))

    async def scenario():
        picture_id = await _picture(DEEZER)
        async with db.SessionLocal() as session:
            row = await session.get(Picture, picture_id)
            row.state, row.file = HELD, str(held / "gone" / "cover.jpg")
            await session.commit()
        return (await store.hold(picture_id))

    row = run(scenario())
    assert row.state == HELD and row.file.startswith("originals/")


def test_the_radio_is_listed_in_its_order_with_its_logos_held_here(held):
    async def scenario():
        async with db.SessionLocal() as session:
            session.add_all([
                RadioStation(name="Second", url="https://b.example/live", sort_order=1),
                RadioStation(name="First", url="http://a.example:8000/", country="HR",
                             logo="https://a.example/logo.png", sort_order=0)])
            await session.commit()
        cookie = await signed_in("ivo")
        async with library(cookie) as client:
            listed = (await client.get("/api/music/radio/stations")).json()["stations"]
            one = await client.get(f"/api/music/radio/stations/{listed[0]['id']}")
            missing = await client.get("/api/music/radio/stations/999999")
        return listed, one.json(), missing.status_code

    listed, one, missing = run(scenario())
    assert [s["name"] for s in listed] == ["First", "Second"]
    assert listed[0]["url"] == "http://a.example:8000/"
    assert listed[0]["logo"].startswith("/api/pictures/")
    assert listed[1]["logo"] is None
    assert one == listed[0]
    assert missing == 404


def test_a_guest_sees_the_pictures_of_the_shelves(held, monkeypatch):
    async def nothing_there(source):
        raise store.Nothing("gone")

    async def scenario():
        picture_id = await _picture("https://example.org/x.jpg")
        cookie = await signed_in("gost", role="guest")
        async with library(cookie) as client:
            return await client.get(f"/api/pictures/{picture_id}")

    monkeypatch.setattr(store, "fetch", nothing_there)
    answer = run(scenario())
    assert answer.status_code == 404
    assert answer.json()["detail"] == "the source has no picture"


def test_a_map_tile_is_kept_and_a_stale_one_outlives_a_silent_server(held, monkeypatch):
    calls = []

    def osm(request):
        calls.append(request.headers["user-agent"])
        if len(calls) > 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kwargs: real(transport=httpx.MockTransport(osm), **kwargs))
    monkeypatch.setattr(tiles, "_client", None)
    assert not tiles.valid(3, 8, 0)
    first = run(tiles.tile(3, 2, 1))
    again = run(tiles.tile(3, 2, 1))
    assert first == again and first.read_bytes() == b"png"
    assert len(calls) == 1 and calls[0].startswith("OPUS-Library")

    monkeypatch.setattr(tiles, "FRESH", 0)
    assert run(tiles.tile(3, 2, 1)).read_bytes() == b"png"
    assert len(calls) == 2
    with pytest.raises(tiles.NoTile):
        run(tiles.tile(3, 2, 2))


def test_a_wanted_picture_waits_its_turn_in_the_list(held):
    async def scenario():
        first = await _picture("https://example.org/a.jpg")
        second = await _picture("https://example.org/b.jpg")
        async with db.SessionLocal() as session:
            (await session.get(Picture, first)).state = HELD
            await session.commit()
        return second, await store.wanted(0, 10), await store.wanted(second, 10)

    second, wanted, after = run(scenario())
    assert wanted == [(second, "https://example.org/b.jpg")]
    assert after == []
