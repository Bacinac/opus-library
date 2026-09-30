import pytest

from conftest import run
from opus.models import MusicFile, Release
from opus.music.library import albumsearch
from opus.music.library.editions import Edition, confirm_edition
from opus.music.metadata import variants
from opus.music.metadata.discogs import DiscogsError
from opus.music.metadata.musicbrainz import MusicBrainzError

HELD = ["Ruža hrvatska", "Moja domovina", "Stari se"]
ROWS = [MusicFile(id=i, path=f"/music/{title}.flac", tag_title=title, tag_track=i)
        for i, title in enumerate(HELD, 1)]


class Discogs:
    def __init__(self, calls, masters=(), details=None, versions=(), tracklists=None):
        self.calls, self.masters, self.details = calls, masters, details or {}
        self.versions, self.tracklists = versions, tracklists or {}

    async def search_masters(self, artist, album):
        self.calls.append(("search_masters", album))
        if isinstance(self.masters, Exception):
            raise self.masters
        return self.masters

    async def master_details(self, master_id):
        self.calls.append(("master_details", master_id))
        got = self.details.get(master_id, {})
        if isinstance(got, Exception):
            raise got
        return got

    async def master_versions(self, master_id):
        self.calls.append(("master_versions", master_id))
        return self.versions

    async def release_tracklist(self, version_id):
        self.calls.append(("release_tracklist", version_id))
        got = self.tracklists[version_id]
        if isinstance(got, Exception):
            raise got
        return [{"title": t} for t in got]


class Calls(list):
    wiki: dict
    mb: dict


@pytest.fixture
def calls(monkeypatch):
    calls = Calls()
    wiki, mb = {}, {"editions": [], "titles": {}}

    async def wikipedia_titles(session, wikidata_id):
        calls.append(("wikipedia", wikidata_id))
        got = wiki[wikidata_id]
        if isinstance(got, Exception):
            raise got
        return got

    async def release_editions(artist, album):
        calls.append(("mb_editions", album))
        if isinstance(mb["editions"], Exception):
            raise mb["editions"]
        return mb["editions"]

    async def release_titles(edition_id):
        calls.append(("mb_titles", edition_id))
        return mb["titles"][edition_id]

    async def note(session, release_id, sighting):
        calls.append(("variant", release_id, sighting))

    monkeypatch.setattr(albumsearch, "wikipedia_titles", wikipedia_titles)
    monkeypatch.setattr(albumsearch.mb, "release_editions", release_editions)
    monkeypatch.setattr(albumsearch.mb, "release_titles", release_titles)
    monkeypatch.setattr(variants, "from_discogs_versions", lambda versions, title: ["a mix"])
    monkeypatch.setattr(variants, "note", note)
    calls.wiki, calls.mb = wiki, mb
    return calls


def _confirm(discogs, release, releases=()):
    return run(confirm_edition(None, discogs, release, [release, *releases],
                               "Thompson", "Bijelo Dugme", len(HELD), ROWS))


def _album(**ids):
    return Release(id=7, title="Bijelo Dugme", **ids)


def test_the_masters_own_count_is_enough(calls):
    discogs = Discogs(calls, details={10: {"track_count": 3}})
    assert _confirm(discogs, _album(discogs_id=10)) == Edition("discogs master")
    assert calls == [("master_details", 10)]


def test_a_siblings_master_and_then_wikipedia(calls):
    calls.wiki["Q1"] = HELD
    discogs = Discogs(calls, details={11: {"track_count": 5}})
    sibling = Release(id=8, title="Bijelo Dugme (Deluxe Edition)", discogs_id=11)
    assert _confirm(discogs, _album(wikidata_id="Q1"), [sibling]) == Edition("wikipedia", HELD)
    assert calls == [("master_details", 11), ("wikipedia", "Q1")]


def test_a_searched_master_then_musicbrainz(calls):
    calls.mb["editions"] = [{"id": "e1", "track_count": 4}, {"id": "e2", "track_count": 3}]
    calls.mb["titles"]["e2"] = HELD
    discogs = Discogs(calls, masters=[{"id": 30, "title": "Something Else"},
                                      {"id": 12, "title": "Bijelo Dugme"}],
                      details={12: DiscogsError("down")})
    assert _confirm(discogs, _album()) == Edition("musicbrainz", HELD)
    assert calls == [("search_masters", "Bijelo Dugme"), ("master_details", 12),
                     ("mb_editions", "Bijelo Dugme"), ("mb_titles", "e2")]


def test_the_last_rung_reads_the_masters_versions(calls):
    calls.wiki["Q2"] = RuntimeError("wikipedia is down")
    calls.mb["editions"] = [{"id": f"e{i}", "track_count": 3} for i in range(2, 6)]
    calls.mb["titles"].update({"e2": ["x"], "e3": ["y"], "e4": ["z"], "e5": HELD})
    discogs = Discogs(calls, versions=[{"id": 101, "format": "Vinyl", "year": "1980"},
                                       {"id": 100, "format": "CD", "year": "2001"}],
                      tracklists={100: DiscogsError("gone"), 101: HELD})
    assert _confirm(discogs, _album(discogs_id=13, wikidata_id="Q2")) == Edition(
        "discogs version", HELD)
    assert calls == [("master_details", 13), ("wikipedia", "Q2"),
                     ("mb_editions", "Bijelo Dugme"), ("mb_titles", "e2"),
                     ("mb_titles", "e3"), ("mb_titles", "e4"), ("master_versions", 13),
                     ("variant", 7, "a mix"), ("release_tracklist", 100),
                     ("release_tracklist", 101)]


def test_nothing_proves_an_edition(calls):
    calls.mb["editions"] = MusicBrainzError("rate limited")
    assert _confirm(None, _album(wikidata_id=None)) is None
    discogs = Discogs(calls, masters=DiscogsError("down"))
    assert _confirm(discogs, _album()) is None
    assert calls == [("mb_editions", "Bijelo Dugme"), ("search_masters", "Bijelo Dugme"),
                     ("mb_editions", "Bijelo Dugme")]
