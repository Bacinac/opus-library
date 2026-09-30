import pytest
from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import Artist, ArtistExternalId, ArtistRelation, EnrichStatus, Image, Release
from opus.music.metadata import enrich


def claim(value, rank="normal", **qualifiers):
    return {"mainsnak": {"datavalue": {"value": value}}, "rank": rank, "qualifiers": qualifiers}


def item(qid):
    return claim({"id": qid})


def year(y):
    return claim({"time": f"+{y}-00-00T00:00:00Z"})


ENTITIES = {
    "Q1": {
        "labels": {"hr": {"value": "Bijelo dugme"}},
        "sitelinks": {"hrwiki": {"title": "Bijelo dugme"}, "enwiki": {"title": "Bijelo Dugme"}},
        "claims": {
            "P31": [item("Q215380")], "P571": [year(1974)], "P495": [item("Q83286")],
            "P740": [item("Q11194")], "P527": [item("Q2"), item("Q4")],
            "P2722": [claim("1000")], "P1953": [claim("12345")], "P434": [claim("mb-dugme")],
            "P18": [claim("Bijelo dugme 1974.jpg")],
        },
    },
    "Q11194": {"labels": {"hr": {"value": "Sarajevo"}}, "claims": {"P17": [item("Q225")]}},
    "Q225": {"labels": {"hr": {"value": "Bosna i Hercegovina"},
                        "en": {"value": "Bosnia and Herzegovina"}}},
    "Q2": {"labels": {"hr": {"value": "Goran Bregović"}},
           "claims": {"P31": [item("Q5")], "P106": [item("Q639669")], "P463": [item("Q1")]}},
    "Q4": {"labels": {"en": {"value": "Some Academy"}}, "claims": {"P31": [item("Q43229")]}},
    "Q10": {
        "labels": {"hr": {"value": "Dino Dvornik"}},
        "sitelinks": {"hrwiki": {"title": "Dino Dvornik"}},
        "claims": {
            "P31": [item("Q5")], "P569": [year(1964)], "P570": [year(2008)],
            "P27": [item("Q224")], "P463": [item("Q20"), item("Q21")], "P2722": [claim("666")],
        },
    },
    "Q224": {"labels": {"hr": {"value": "Hrvatska"}, "en": {"value": "Croatia"}}},
    "Q20": {"labels": {"hr": {"value": "Bijelo dugme"}},
            "claims": {"P31": [item("Q215380")], "P527": [item("Q99")]}},
    "Q21": {"labels": {"hr": {"value": "Kineski zid"}},
            "claims": {"P31": [item("Q215380")], "P527": [item("Q10")]}},
    "Q30": {"labels": {"en": {"value": "Vojko V"}},
            "sitelinks": {"hrwiki": {"title": "Vojko V"}},
            "claims": {"P31": [item("Q5")], "P106": [item("Q2252262")], "P27": [item("Q224")]}},
}

WIKITEXT = {("en", "Bijelo Dugme"): "{{Infobox musical artist\n| name = Bijelo Dugme\n"
                                    "| past_members = [[Željko Bebek]]<br />Goran Bregović\n}}\n"}
EXTRACTS = {("en", "Bijelo Dugme"): {"extract": "Bijelo Dugme was a rock band.",
                                     "url": "https://en.wikipedia.org/wiki/Bijelo_Dugme"},
            ("hr", "Dino Dvornik"): {"extract": "Dino Dvornik bio je pjevač.",
                                     "url": "https://hr.wikipedia.org/wiki/Dino_Dvornik"}}
PAGES = {1000: "Bijelo Dugme", 555: "Dino Dvornik Tribute", 666: "Dino Dvornik"}


class Wikidata:
    def __init__(self):
        self.asked: list[tuple] = []

    async def get_entities(self, qids):
        return {q: ENTITIES[q] for q in qids if q in ENTITIES}

    async def qid_by_deezer_id(self, deezer_id):
        return {1000: "Q1"}.get(deezer_id)

    async def search(self, text):
        self.asked.append(("search", text))
        return []

    async def albums_by_performer(self, qid):
        return []

    async def wikipedia_extract(self, lang, title):
        return EXTRACTS.get((lang, title))

    async def wikipedia_wikitext(self, lang, title):
        return WIKITEXT.get((lang, title), "")

    async def wikipedia_titles_named(self, lang, name):
        self.asked.append(("titles", lang, name))
        return []

    async def artist_studio_albums(self, qid):
        return {"Q1": ["Kad bi' bio bijelo dugme"]}.get(qid)


class Deezer:
    async def get_artist(self, deezer_id):
        return {"name": PAGES.get(deezer_id, "")}

    async def search_artists(self, name):
        return [{"id": 999, "name": "Vojko V", "nb_album": 4}] if name == "Vojko V" else []

    async def close(self):
        pass


class MusicBrainz:
    async def find_artist(self, name):
        return None

    async def artist_relations(self, mb_id):
        return {"mb-dugme": [{"name": "Goran Bregović", "person": True}]}.get(mb_id, [])


@pytest.fixture
def sources(monkeypatch, clean):
    chained = []
    monkeypatch.setattr(enrich, "DeezerClient", Deezer)
    monkeypatch.setattr(enrich, "MusicBrainzClient", MusicBrainz)
    monkeypatch.setattr(enrich.discography, "spawn_sync", chained.append)
    return chained


async def _artists(*rows):
    async with db.SessionLocal() as session:
        for row in rows:
            session.add(row)
            await session.flush()
        await session.commit()


async def _shelf() -> dict:
    async with db.SessionLocal() as session:
        artists = {a.id: a for a in (await session.execute(select(Artist))).scalars()}
        names = {a.id: a.name for a in artists.values()}
        return {
            "artists": {a.id: (a.name, a.wikidata_id, a.deezer_id, a.tidal_id, a.artist_type,
                               a.begin_year, a.end_year, (a.country, a.country_hr), a.bio, a.bio_url,
                               a.image_url, a.enrich_status, a.monitored,
                               a.wiki_studio_albums)
                        for a in artists.values()},
            "relations": sorted((names[r.artist_id], names[r.related_artist_id], r.source)
                                for r in (await session.execute(select(ArtistRelation))).scalars()),
            "external": sorted((names[e.artist_id], e.source, e.external_id)
                               for e in (await session.execute(select(ArtistExternalId))).scalars()),
            "images": [(names[i.entity_id], i.url) for i in (await session.execute(select(Image))).scalars()],
            "releases": sorted((names[r.artist_id], r.title)
                               for r in (await session.execute(select(Release))).scalars()),
        }


def test_a_band_is_resolved_and_its_line_up_gathered(sources):
    run(_artists(
        Artist(id=101, name="BIJELO DUGME", deezer_id=1000, tidal_id=77),
        Artist(id=102, name="Goran Bregović"),
        Artist(id=103, name="Bijelo dugme (dup)", wikidata_id="Q1", monitored=False),
        Release(artist_id=101, title="Kad bi' bio bijelo dugme", release_date="1974-11-01"),
        Release(artist_id=103, title="Eto! Baš hoću!", release_date="1976-01-01"),
    ))
    client = Wikidata()
    run(enrich._enrich(101, None, client))
    shelf = run(_shelf())
    assert shelf["artists"][101] == (
        "Bijelo dugme", "Q1", 1000, 77, "group", 1974, None, ("Bosnia and Herzegovina", "Bosna i Hercegovina"),
        "Bijelo Dugme was a rock band.", "https://en.wikipedia.org/wiki/Bijelo_Dugme",
        "https://commons.wikimedia.org/wiki/Special:FilePath/Bijelo%20dugme%201974.jpg?width=1000",
        EnrichStatus.RESOLVED, True, ["Kad bi' bio bijelo dugme"])
    assert 103 not in shelf["artists"]
    assert shelf["artists"][102][1] == "Q2"
    bebek = [a for a in shelf["artists"].values() if a[0] == "Željko Bebek"]
    assert bebek == [("Željko Bebek", None, None, None, "person", None, None, (None, None), None, None,
                      None, EnrichStatus.PENDING, False, None)]
    assert shelf["relations"] == [
        ("Goran Bregović", "Bijelo dugme", "wikidata"),
        ("Željko Bebek", "Bijelo dugme", "wikipedia"),
    ]
    assert shelf["external"] == [("Bijelo dugme", "discogs", "12345"),
                                 ("Bijelo dugme", "musicbrainz", "mb-dugme")]
    assert shelf["images"] == [("Bijelo dugme", shelf["artists"][101][10])]
    assert [artist for artist, _ in shelf["releases"]] == ["Bijelo dugme", "Bijelo dugme"]
    assert sources == [101]


def test_a_person_takes_the_verified_claim_and_only_bands_that_list_them(sources):
    run(_artists(Artist(id=110, name="Dino Dvornik", deezer_id=555, tidal_id=88,
                        wikidata_id="Q10"),
                 Artist(id=111, name="Bijelo dugme", wikidata_id="Q20")))
    run(enrich._enrich(110, None, Wikidata(), chain_discography=False))
    shelf = run(_shelf())
    assert shelf["artists"][110] == (
        "Dino Dvornik", "Q10", 666, None, "person", 1964, 2008, ("Croatia", "Hrvatska"),
        "Dino Dvornik bio je pjevač.", "https://hr.wikipedia.org/wiki/Dino_Dvornik",
        None, EnrichStatus.RESOLVED, True, None)
    assert shelf["relations"] == [("Dino Dvornik", "Kineski zid", "wikidata")]
    kineski = [a for a in shelf["artists"].values() if a[0] == "Kineski zid"]
    assert kineski[0][1:5] == ("Q21", None, None, "group") and kineski[0][12] is False
    assert sources == []


def test_a_deezer_page_is_adopted_by_name(sources):
    run(_artists(Artist(id=130, name="Vojko Vrućina", wikidata_id="Q30", tidal_id=5),
                 Release(artist_id=130, title="Groovy", release_date="2004-02-02")))
    run(enrich._enrich(130, None, Wikidata(), chain_discography=False))
    artist = run(_shelf())["artists"][130]
    assert artist[:5] == ("Vojko V", "Q30", 999, 5, "person")
    assert artist[5] == 2004 and artist[7] == ("Croatia", "Hrvatska")


def test_an_artist_nobody_can_resolve_is_left_unresolved_but_dated(sources):
    run(_artists(Artist(id=140, name="Mile i Putnici"),
                 Release(artist_id=140, title="Prvi", release_date="2009-03-01"),
                 Release(artist_id=140, title="Drugi", release_date="2012-03-01")))
    client = Wikidata()
    run(enrich._enrich(140, None, client))
    artist = run(_shelf())["artists"][140]
    assert (artist[1], artist[5], artist[11]) == (None, 2009, EnrichStatus.UNRESOLVED)
    assert client.asked == [("search", "Mile i Putnici"), ("titles", "hr", "Mile i Putnici"),
                            ("titles", "sr", "Mile i Putnici"), ("titles", "en", "Mile i Putnici")]
    assert sources == []
