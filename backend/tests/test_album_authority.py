import pytest
from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import Artist, MusicFile, Release, Track, WikiAlbumCache
from opus.music.metadata import authority

NONE = {"record_type": None, "tracklist": None, "external_ids": {}}

ALBUMS = [
    {"qid": "Q1", "label": "MTV Unplugged", "date": "1994-11-01", "record_type": "live"},
    {"qid": "Q2", "label": "Killers", "date": "1981", "record_type": "album"},
    {"qid": "Q3", "label": "Pin Ups", "date": "1973-10-19", "record_type": "album"},
    {"qid": "Q4", "label": "Vojko", "date": None, "record_type": "album"},
    {"qid": "Q5", "label": "The Albums", "date": "2008", "record_type": "compilation"},
    {"qid": "Q6", "label": None, "date": "1999", "record_type": "album"},
    {"qid": "Q7", "label": "Riding With The King", "date": "2000-06-13", "record_type": "album"},
    {"qid": "Q8", "label": "Long " * 101, "date": "1990", "record_type": "album"},
    {"qid": "Q9", "label": "Blue Train", "date": "1957", "record_type": None},
    {"qid": "Q10", "label": "Nevermind", "date": "1991-09-24", "record_type": "album"},
    {"qid": "Q11", "label": "In Utero", "date": "1993", "record_type": "album"},
    {"qid": "Q12", "label": "Bleach", "date": "1989-06-15", "record_type": "album"},
    {"qid": "Q13", "label": "Incesticide", "date": "1992", "record_type": "compilation"},
    {"qid": "Q14", "label": "Smells Like Teen Spirit", "date": "1991", "record_type": "album"},
]

ARTICLES = {
    "Q2": NONE, "Q3": RuntimeError("wikipedia is down"), "Q8": NONE, "Q9": NONE,
    "Q10": {"record_type": "video", "tracklist": None,
            "external_ids": {"deezer": "123", "spotify": "sp10", "discogs": "not-a-number"}},
    "Q11": {"record_type": None, "tracklist": None,
            "external_ids": {"deezer": "123", "discogs": "555"}},
    "Q12": {"record_type": None,
            "tracklist": ["Blew", "Floyd the Barber", "Paper Cuts!", "About a Girl"],
            "external_ids": {}},
    "Q13": {"record_type": None, "tracklist": ["Dive"], "external_ids": {}},
}


class Wikidata:
    said: list[tuple] = []
    albums: list[dict] = []

    def __init__(self):
        self.said.append(("open",))

    async def albums_by_performer(self, qid):
        self.said.append(("albums", qid))
        return self.albums

    async def wikipedia_album_info(self, qid):
        self.said.append(("article", qid))
        answer = ARTICLES[qid]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self):
        self.said.append(("close",))


@pytest.fixture
def wikidata(monkeypatch, clean):
    monkeypatch.setattr(authority.wd, "WikidataClient", Wikidata)
    monkeypatch.setattr(authority, "_wikipedia_info", {})
    monkeypatch.setattr(Wikidata, "said", [])
    monkeypatch.setattr(Wikidata, "albums", ALBUMS)
    return Wikidata


async def _setup(*rows):
    async with db.SessionLocal() as session:
        for row in rows:
            session.add(row)
            await session.flush()
        await session.commit()


async def _rule(artist_qid: str | None, artist_id: int) -> tuple[int, int]:
    async with db.SessionLocal() as session:
        releases = list((await session.execute(
            select(Release).where(Release.artist_id == artist_id).order_by(Release.id)
        )).scalars())
        answer = await authority._wikidata_album_info(session, artist_qid, releases)
        await session.commit()
        return answer


async def _shelf(artist_id: int) -> dict[int, tuple]:
    async with db.SessionLocal() as session:
        return {r.id: (r.title, r.wikidata_id, r.release_date, r.record_type, r.deezer_id,
                       r.spotify_id, r.discogs_id)
                for r in (await session.execute(
                    select(Release).where(Release.artist_id == artist_id))).scalars()}


async def _tracks(release_id: int) -> list[str]:
    async with db.SessionLocal() as session:
        return list((await session.execute(
            select(Track.title).where(Track.release_id == release_id).order_by(Track.position)
        )).scalars())


def test_the_authority_names_dates_types_and_links_the_records_it_knows(wikidata):
    run(_setup(
        Artist(id=1, name="Nirvana", wikidata_id="Q100"),
        Artist(id=2, name="B.B. King", wikidata_id="Q200"),
        Release(id=90, artist_id=2, title="Riding With The King", wikidata_id="Q7"),
        Release(id=91, artist_id=2, title="Somebody's Record", discogs_id=555),
        Release(id=1, artist_id=1, title="MTV Unplugged (Live)", release_date="1994"),
        Release(id=2, artist_id=1, title="Killers (2015 Remaster)", release_date="2018-01-01"),
        Release(id=3, artist_id=1, title="Pinups (Remastered)", release_date="2015"),
        Release(id=4, artist_id=1, title="Dvojko"),
        Release(id=5, artist_id=1, title="The Album", release_date="1977"),
        Release(id=7, artist_id=1, title="Riding With The King", release_date="2000"),
        Release(id=8, artist_id=1, title="Short", wikidata_id="Q8", release_date="2001"),
        Release(id=9, artist_id=1, title="Blue Train (Mono)", wikidata_id="Q9",
                release_date="1957-09-15", record_type="ep"),
        Release(id=10, artist_id=1, title="Nevermind", release_date="1991"),
        Release(id=11, artist_id=1, title="In Utero", release_date="1993"),
        Release(id=12, artist_id=1, title="Bleach", release_date="1989"),
        Release(id=13, artist_id=1, title="Bleach", release_date="1989"),
        Release(id=14, artist_id=1, title="Incesticide", wikidata_id="Q13"),
        Release(id=15, artist_id=1, title="Incesticide", release_date="1992"),
        Release(id=16, artist_id=1, title="Smells Like Teen Spirit", record_type="single",
                release_date="1991"),
        Track(id=131, release_id=13, position=1, title="Blew"),
        Track(id=132, release_id=13, position=2, title="Floyd the Barbar"),
        Track(id=133, release_id=13, position=3, title="Paper Cuts", title_manual=True),
        Track(id=134, release_id=13, position=4, title="Something Else Entirely"),
        Track(id=151, release_id=15, position=1, title="Dive"),
        Track(id=152, release_id=15, position=2, title="Sliver"),
        MusicFile(path="/music/Nirvana/Bleach/01.flac", track_id=131),
        MusicFile(path="/music/Nirvana/Bleach/02.flac", track_id=132),
        MusicFile(path="/music/Nirvana/Incesticide/01.flac", track_id=151),
    ))

    assert run(_rule("Q100", 1)) == (7, 1)

    shelf = run(_shelf(1))
    assert shelf == {
        1: ("MTV Unplugged", "Q1", "1994-11-01", "live", None, None, None),
        2: ("Killers", "Q2", "1981", "album", None, None, None),
        3: ("Pin Ups", "Q3", "1973-10-19", "album", None, None, None),
        4: ("Dvojko", None, None, None, None, None, None),
        5: ("The Album", None, "1977", None, None, None, None),
        7: ("Riding with the King", None, "2000", None, None, None, None),
        8: ("Short", "Q8", "1990", "album", None, None, None),
        9: ("Blue Train", "Q9", "1957-09-15", "ep", None, None, None),
        10: ("Nevermind", "Q10", "1991-09-24", "video", 123, "sp10", None),
        11: ("In Utero", "Q11", "1993", "album", None, None, None),
        12: ("Bleach", None, "1989", None, None, None, None),
        13: ("Bleach", "Q12", "1989-06-15", "album", None, None, None),
        14: ("Incesticide", None, "1992", "compilation", None, None, None),
        15: ("Incesticide", "Q13", "1992", "compilation", None, None, None),
        16: ("Smells Like Teen Spirit", None, "1991", "single", None, None, None),
    }
    assert run(_tracks(13)) == ["Blew", "Floyd the Barber", "Paper Cuts",
                                "Something Else Entirely"]
    assert run(_tracks(15)) == ["Dive", "Sliver"]
    assert wikidata.said == [
        ("open",), ("albums", "Q100"),
        ("article", "Q8"), ("article", "Q9"), ("article", "Q12"), ("article", "Q2"),
        ("article", "Q3"), ("article", "Q10"), ("article", "Q11"), ("article", "Q13"),
        ("close",),
    ]

    async def cached():
        async with db.SessionLocal() as session:
            return sorted((await session.execute(select(WikiAlbumCache.qid))).scalars())

    assert run(cached()) == ["Q10", "Q11", "Q12", "Q13", "Q2", "Q8", "Q9"]


def test_nothing_to_rule_on_asks_nobody_and_an_empty_answer_closes_the_client(wikidata):
    run(_setup(
        Artist(id=1, name="Singles Band", wikidata_id="Q100"),
        Artist(id=2, name="Albums Band", wikidata_id="Q200"),
        Artist(id=3, name="Nobody"),
        Release(id=1, artist_id=1, title="A Single", record_type="single"),
        Release(id=2, artist_id=1, title="An EP", record_type="ep"),
        Release(id=3, artist_id=2, title="Nevermind"),
    ))

    assert run(_rule(None, 2)) == (0, 0)
    assert run(_rule("Q100", 1)) == (0, 0)
    assert run(_rule("Q300", 3)) == (0, 0)
    assert wikidata.said == []

    wikidata.albums = [{"qid": "Q6", "label": None, "date": "1999", "record_type": "album"}]
    assert run(_rule("Q200", 2)) == (0, 0)
    assert wikidata.said == [("open",), ("albums", "Q200"), ("close",)]
    assert run(_shelf(2)) == {3: ("Nevermind", None, None, None, None, None, None)}
