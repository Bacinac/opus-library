import pytest
from sqlalchemy.dialects.postgresql import insert

from conftest import run
from opus import db
from opus.models import Artist, Release, Setting, Track
from opus.music.metadata import tracklists


class Asked:
    def __init__(self):
        self.order: list[str] = []
        self.closed: list[str] = []


def fake(asked: Asked, name: str, method: str, answer):
    class Client:
        def __init__(self, *args):
            pass

        async def close(self):
            asked.closed.append(name)

    async def fetch(self, *args):
        asked.order.append(name)
        if isinstance(answer, Exception):
            raise answer
        return answer

    setattr(Client, method, fetch)
    return Client


DEEZER = [{"id": 21, "title": "Uno", "track_position": 1, "duration": 100},
          {"id": 22, "title": "Dos", "track_position": 2, "duration": 110}]
DISCOGS = [{"position": 1, "title": "A1", "duration_sec": 60}]
SPOTIFY = [{"position": 1, "title": "S1", "duration_sec": 70}]


@pytest.fixture
def sources(monkeypatch):
    asked = Asked()

    def given(deezer=None, discogs=None, spotify=None):
        monkeypatch.setattr(tracklists, "DeezerClient",
                            fake(asked, "deezer", "get_album_tracks", deezer))
        monkeypatch.setattr(tracklists, "DiscogsClient",
                            fake(asked, "discogs", "master_tracklist", discogs))
        monkeypatch.setattr(tracklists, "SpotifyClient",
                            fake(asked, "spotify", "album_tracks", spotify))
        return asked

    return given


async def _release(**ids) -> int:
    async with db.SessionLocal() as session:
        for key, value in (("discogs_token", "d"), ("spotify_client_id", "s"),
                           ("spotify_client_secret", "t")):
            await session.execute(insert(Setting).values(key=key, value=value))
        artist = Artist(name="Somebody")
        session.add(artist)
        await session.flush()
        release = Release(artist_id=artist.id, title="Record", **ids)
        session.add(release)
        await session.commit()
        return release.id


async def _ensure(release_id: int):
    async with db.SessionLocal() as session:
        release = await session.get(Release, release_id)
        tracks = await tracklists.ensure_tracks(session, release)
        await session.commit()
        return [(t.position, t.title, t.deezer_id) for t in tracks], release.track_count


EVERY_ID = {"deezer_id": 2, "discogs_id": 3, "spotify_id": "4"}


def test_the_first_source_that_answers_is_taken(clean, sources):
    asked = sources(deezer=DEEZER, discogs=DISCOGS)
    tracks, count = run(_ensure(run(_release(**EVERY_ID))))
    assert tracks == [(1, "Uno", 21), (2, "Dos", 22)]
    assert count == 2
    assert asked.order == ["deezer"] and asked.closed == ["deezer"]


def test_a_source_that_fails_hands_on_to_the_next(clean, sources):
    asked = sources(deezer=RuntimeError("deezer is down"), discogs=DISCOGS)
    tracks, count = run(_ensure(run(_release(**EVERY_ID))))
    assert tracks == [(1, "A1", None)]
    assert asked.order == ["deezer", "discogs"] and asked.closed == ["deezer", "discogs"]


def test_a_source_with_nothing_hands_on_to_the_next(clean, sources):
    asked = sources(deezer=[], discogs=DataError(), spotify=SPOTIFY)
    tracks, _ = run(_ensure(run(_release(**EVERY_ID))))
    assert tracks == [(1, "S1", None)]
    assert asked.order == ["deezer", "discogs", "spotify"]


def test_only_the_sources_that_know_the_release_are_asked(clean, sources):
    asked = sources(discogs=DISCOGS)
    tracks, _ = run(_ensure(run(_release(discogs_id=3))))
    assert tracks == [(1, "A1", None)]
    assert asked.order == ["discogs"]


def test_nothing_anywhere_fails_loud_with_every_answer(clean, sources):
    asked = sources(deezer=RuntimeError("deezer is down"),
                    discogs=RuntimeError("discogs refused"), spotify=[])
    release_id = run(_release(**EVERY_ID))
    with pytest.raises(tracklists.DiscographyError) as failed:
        run(_ensure(release_id))
    said = str(failed.value)
    for part in ("deezer: deezer is down", "discogs: discogs refused", "spotify: no tracks"):
        assert part in said
    assert asked.closed == ["deezer", "discogs", "spotify"]


def test_tracks_already_held_are_not_asked_for(clean, sources):
    asked = sources(deezer=DEEZER)
    release_id = run(_release(**EVERY_ID))

    async def hold():
        async with db.SessionLocal() as session:
            session.add(Track(release_id=release_id, position=1, title="Held"))
            await session.commit()

    run(hold())
    tracks, _ = run(_ensure(release_id))
    assert tracks == [(1, "Held", None)]
    assert asked.order == []


class DataError(Exception):
    pass
