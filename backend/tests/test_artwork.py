import httpx
import pytest

from conftest import run
from opus.music.metadata.artwork import _itunes_album_candidates

# iTunes' own real answer to "Pink Floyd The Dark Side of the Moon":
# none of its top 5 is the album itself — two are Pink Floyd's own other albums,
# one is a same-titled EP by an unrelated K-pop artist, one a reggae cover album
# by an unrelated artist, and a genuine match is added separately below.
RESULTS = [
    {"artistName": "Pink Floyd", "collectionName": "The Wall",
     "artworkUrl100": "https://mzstatic/wall/100x100bb.jpg"},
    {"artistName": "Pink Floyd", "collectionName": "Meddle (2016 Remaster)",
     "artworkUrl100": "https://mzstatic/meddle/100x100bb.jpg"},
    {"artistName": "Moon Byul", "collectionName": "Dark Side of the Moon - EP",
     "artworkUrl100": "https://mzstatic/moonbyul/100x100bb.jpg"},
    {"artistName": "Easy Star All-Stars",
     "collectionName": "Dub Side of the Moon (A Reggae Version of Pink Floyd's Dark Side of the Moon)",
     "artworkUrl100": "https://mzstatic/dubside/100x100bb.jpg"},
    {"artistName": "Pink Floyd", "collectionName": "The Dark Side of the Moon (50th Anniversary) [2023 Remaster]",
     "artworkUrl100": "https://mzstatic/dsotm50th/100x100bb.jpg"},
]


@pytest.fixture
def itunes(monkeypatch):
    real = httpx.AsyncClient

    def handler(request):
        return httpx.Response(200, json={"results": RESULTS})

    def client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def test_neither_a_strangers_album_nor_the_right_artists_other_one_gets_through(itunes):
    # blended "artist album" scoring let either half's strong match carry a
    # mismatch in the other half: "Moon Byul Dark Side of the Moon - EP"
    # blended to 84 on the shared album title (its artist alone scores 32
    # against "Pink Floyd"); "Pink Floyd The Wall" blended to 85 on the shared
    # artist (its album alone scores 55 against "The Dark Side of the Moon").
    # Only the 50th-anniversary remaster is actually the album asked for —
    # token_set_ratio takes it as a clean superset rather than penalising it
    # for every word it adds, the way a plain ratio would.
    candidates = run(_itunes_album_candidates("Pink Floyd", "The Dark Side of the Moon"))
    assert candidates == [{
        "url": "https://mzstatic/dsotm50th/3000x3000bb.jpg", "width": 3000, "height": 3000,
    }]
