"""The words to a record: asked for a whole album at once, and the difference
between a song nobody has transcribed and an archive that could not be asked."""

import httpx
import pytest
from sqlalchemy import select

from conftest import library, run, signed_in
from opus import db
from opus.models import Artist, Release, ReleaseStatus, Track, TrackLyrics
from opus.music import lyrics

SYNCED = "[00:12.00]One line\n[00:18.50]Another\n"


async def _record() -> None:
    async with db.SessionLocal() as session:
        session.add(Artist(id=1, name="Haustor"))
        await session.flush()
        session.add(Release(id=11, artist_id=1, title="Treći Svijet",
                            status=ReleaseStatus.COMPLETE))
        await session.flush()
        session.add_all([
            Track(id=101, release_id=11, position=1, title="Radio", duration_sec=220),
            Track(id=102, release_id=11, position=2, title="Moja Prva Ljubav", duration_sec=180),
        ])
        await session.commit()


async def _tracks(session):
    from sqlalchemy.orm import selectinload
    result = await session.execute(
        select(Track).where(Track.release_id == 11).order_by(Track.position).options(
            selectinload(Track.files),
            selectinload(Track.release).selectinload(Release.artist)))
    return list(result.scalars())


def test_album_asks_once_for_every_song(clean, monkeypatch):
    """The first song has words and the second has none. Both answers are
    written down, so opening the record again asks the archive nothing."""
    run(_record())
    asked: list[str] = []

    async def archive(artist, title, album, seconds):
        asked.append(title)
        return {"syncedLyrics": SYNCED} if title == "Radio" else None

    monkeypatch.setattr(lyrics, "_archive", archive)

    async def go():
        async with db.SessionLocal() as session:
            first = await lyrics.album(session, await _tracks(session))
        async with db.SessionLocal() as session:
            again = await lyrics.album(session, await _tracks(session))
            rows = (await session.execute(select(TrackLyrics))).scalars().all()
        return first, again, rows

    first, again, rows = run(go())
    assert first == {"words": [101], "unknown": []}
    assert again == first
    assert sorted(asked) == ["Moja Prva Ljubav", "Radio"]
    assert {row.track_id: row.source for row in rows} == {101: "lrclib", 102: "none"}


def test_an_archive_that_could_not_be_asked_is_not_a_song_without_words(clean, monkeypatch):
    """A refused request used to be written down as "none", which stands for the
    month RETRY_MISS lasts. The song is named as unknown instead, and asked
    about again the next time anybody opens the record."""
    run(_record())
    refuse = True

    async def archive(artist, title, album, seconds):
        if refuse:
            raise httpx.ConnectError("lrclib is down")
        return {"plainLyrics": "Some words"}

    monkeypatch.setattr(lyrics, "_archive", archive)

    async def down():
        async with db.SessionLocal() as session:
            answer = await lyrics.album(session, await _tracks(session))
            rows = (await session.execute(select(TrackLyrics))).scalars().all()
        return answer, rows

    answer, rows = run(down())
    assert answer == {"words": [], "unknown": [101, 102]}
    assert rows == []

    refuse = False

    async def up():
        async with db.SessionLocal() as session:
            return await lyrics.album(session, await _tracks(session))

    assert run(up()) == {"words": [101, 102], "unknown": []}


def test_one_song_says_out_loud_that_it_could_not_be_looked_up(clean, monkeypatch):
    """And the same for a single song, which the screen answers with an offer to
    look again rather than with "this song has no words"."""
    run(_record())

    async def archive(artist, title, album, seconds):
        raise httpx.ReadTimeout("no answer")

    monkeypatch.setattr(lyrics, "_archive", archive)

    async def ask():
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return await client.get("/api/music/tracks/101/lyrics")

    assert run(ask()).status_code == 502


def test_a_record_says_which_of_its_songs_have_words(clean, monkeypatch):
    run(_record())

    async def archive(artist, title, album, seconds):
        return {"syncedLyrics": SYNCED} if title == "Radio" else None

    monkeypatch.setattr(lyrics, "_archive", archive)

    async def ask():
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return await client.get("/api/music/releases/11/lyrics")

    answer = run(ask())
    assert answer.status_code == 200
    assert answer.json() == {"words": [101], "unknown": []}


def test_a_record_nobody_holds_is_not_a_record(clean):
    run(_record())

    async def ask():
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return await client.get("/api/music/releases/99/lyrics")

    assert run(ask()).status_code == 404


def test_a_lookup_the_archive_errors_on_is_still_asked_of_the_search(clean, monkeypatch):
    """LRCLIB answers 503 to the exact lookup for songs its search then hands
    over without complaint. Giving up there lost the words to a song that was
    there all along."""
    real = httpx.AsyncClient
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.path)
        if request.url.path.endswith("/get"):
            return httpx.Response(503, text="try again")
        return httpx.Response(200, json=[{"syncedLyrics": SYNCED, "duration": 220.0}])

    def client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    run(_record())

    async def go():
        async with db.SessionLocal() as session:
            tracks = await _tracks(session)
            return await lyrics.album(session, tracks[:1])

    assert run(go()) == {"words": [101], "unknown": []}
    assert asked == ["/api/get", "/api/search"]


@pytest.mark.parametrize("lrc, lines", [
    ("[00:10.00]A\n[00:20.00]B", 2),
    ("[00:10.00][00:30.00]Chorus", 2),
    ("no timings at all", 0),
])
def test_an_lrc_is_read_line_by_line(lrc, lines):
    assert len(lyrics.parse(lrc)) == lines
