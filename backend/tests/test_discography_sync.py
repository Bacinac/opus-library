import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from conftest import run
from opus import db
from opus.models import (Artist, ArtistExternalId, MusicFile, Release, ReleaseStatus, Setting,
                         Track)
from opus.music.metadata import discography
from opus.music.metadata.deezer import DeezerError, DeezerNotFound
from opus.music.metadata.discogs import DiscogsError
from opus.music.metadata.spotify import SpotifyCooldown, SpotifyError
from opus.music.metadata.tracklists import DiscographyError


class Sources:
    def __init__(self):
        self.asked: list[tuple] = []
        self.pages: dict[int, list[dict]] = {}
        self.albums: dict[int, dict | Exception] = {}
        self.discogs_artist: int | None = None
        self.masters: list[dict] | Exception = []
        self.details: dict[int, dict | Exception] = {}
        self.stored: list[tuple] = []
        self.spotify: list[dict] | Exception = []
        self.dated = (0, 0)
        self.identified: dict[str, str] = {}
        self.closed: list[str] = []
        sources = self

        class Deezer:
            async def get_artist_albums(self, deezer_id):
                return sources.pages[deezer_id]

            async def get_album(self, album_id):
                sources.asked.append(("deezer album", album_id))
                answer = sources.albums[album_id]
                if isinstance(answer, Exception):
                    raise answer
                return answer

            async def close(self):
                sources.closed.append("deezer")

        class Discogs:
            def __init__(self, token):
                sources.asked.append(("discogs", token))

            async def find_artist_id(self, name):
                sources.asked.append(("discogs search", name))
                return sources.discogs_artist

            async def cached_artist_masters(self, session, artist_id):
                sources.asked.append(("discogs masters", artist_id))
                if isinstance(sources.masters, Exception):
                    raise sources.masters
                return sources.masters

            async def master_details(self, master_id):
                sources.asked.append(("discogs master", master_id))
                answer = sources.details[master_id]
                if isinstance(answer, Exception):
                    raise answer
                return answer

            async def store_masters(self, session, artist_id, masters):
                sources.stored.append((artist_id, [dict(m) for m in masters]))

            async def close(self):
                sources.closed.append("discogs")

        class Spotify:
            def __init__(self, client_id, secret):
                sources.asked.append(("spotify", client_id, secret))

            async def artist_albums(self, spotify_id):
                sources.asked.append(("spotify albums", spotify_id))
                if isinstance(sources.spotify, Exception):
                    raise sources.spotify
                return sources.spotify

            async def close(self):
                sources.closed.append("spotify")

        self.Deezer, self.Discogs, self.Spotify = Deezer, Discogs, Spotify

    async def retrack(self, session, releases):
        self.asked.append(("retrack", sorted(r.title for r in releases)))
        return 2

    async def wikidata(self, session, artist_qid, releases):
        self.asked.append(("wikidata", artist_qid))
        for release in releases:
            if release.title in self.identified:
                release.wikidata_id = self.identified[release.title]
        return self.dated


@pytest.fixture
def sources(monkeypatch, clean):
    made = Sources()
    monkeypatch.setattr(discography, "DeezerClient", made.Deezer)
    monkeypatch.setattr(discography, "DiscogsClient", made.Discogs)
    monkeypatch.setattr(discography, "SpotifyClient", made.Spotify)
    monkeypatch.setattr(discography, "_refresh_deezer_tracks", made.retrack)
    monkeypatch.setattr(discography, "_wikidata_album_info", made.wikidata)
    monkeypatch.setattr(discography, "_kept_strays", set())
    return made


def page(principal: int, *rows: tuple[int, str, str]) -> tuple[list[dict], dict[int, dict]]:
    albums = [{"id": i, "title": t, "release_date": d, "record_type": "album",
               "cover_medium": f"https://deezer/{i}.jpg"} for i, t, d in rows]
    return albums, {i: {"artist": {"id": principal}} for i, _, _ in rows}


async def _setup(*rows, settings: dict[str, str] | None = None):
    async with db.SessionLocal() as session:
        for key, value in (settings or {}).items():
            await session.execute(insert(Setting).values(key=key, value=value))
        for row in rows:
            session.add(row)
            await session.flush()
        await session.commit()


async def _shelf(artist_id: int) -> tuple[list[tuple], list[tuple]]:
    async with db.SessionLocal() as session:
        releases = sorted(
            (r.title, r.deezer_id, r.discogs_id, r.spotify_id, r.wikidata_id, r.release_date,
             r.record_type, r.cover_url)
            for r in (await session.execute(
                select(Release).where(Release.artist_id == artist_id))).scalars())
        external = sorted(
            (e.source, e.external_id)
            for e in (await session.execute(
                select(ArtistExternalId).where(ArtistExternalId.artist_id == artist_id)
            )).scalars())
        return releases, external


def test_a_known_catalogue_is_folded_in_and_its_strays_and_bloat_go(sources, caplog):
    albums, details = page(
        500,
        (9001, "Apsurdni Svijet", "1980-01-01"),
        (9002, "U Širokom Svijetu", "2002-05-05"),
        (9003, "Ruke", "2004-03-03"),
        (9004, "Mali Čovjek", "2005-01-01"),
        (9005, "Shared Compilation", "2006-01-01"),
        (9006, "Plan B", "2007-01-01"),
        (9007, "Removed", "2008-01-01"),
        (9008, "Unreadable", "2008-02-02"),
        (9009, "Brod U Boci", "2009-09-09"),
    )
    sources.pages[500] = albums
    sources.albums.update(details)
    sources.albums.update({
        9004: {"artist": {"id": 999}}, 9006: {"artist": {"id": 999}},
        9007: DeezerNotFound("gone"), 9008: DeezerError("rate limited"),
        9100: {"artist": {"id": 777}}, 9101: {"artist": {"id": 500}},
        9102: DeezerNotFound("gone"), 9103: {},
    })
    sources.identified = {"Wikidata Bootleg": "Q7102"}
    sources.dated = (3, 4)
    run(_setup(
        Artist(id=1, name="Darko Rundek", deezer_id=500, wikidata_id="Q500",
               artist_type="person", begin_year=1956),
        Artist(id=2, name="Various", deezer_id=501),
        Release(artist_id=2, title="Shared Compilation", deezer_id=9005),
        Release(artist_id=1, title="U Širokom Svijetu", discogs_id=7002, release_date="2002"),
        Release(artist_id=1, title="Ruke", deezer_id=9003, release_date="2004"),
        Release(artist_id=1, title="Mali Čovjek", deezer_id=9004),
        Release(id=40, artist_id=1, title="Plan B", deezer_id=9006,
                status=ReleaseStatus.WANTED),
        Release(artist_id=1, title="Ghost", deezer_id=9100),
        Release(artist_id=1, title="Still Ours", deezer_id=9101, release_date="1999-01-01"),
        Release(artist_id=1, title="Still Ours", discogs_id=7201, release_date="1995"),
        Release(artist_id=1, title="Unknown Now", deezer_id=9102),
        Release(artist_id=1, title="Nobody Credited", deezer_id=9103),
        Release(artist_id=1, title="Bootleg", discogs_id=7100),
        Release(id=41, artist_id=1, title="Held Bootleg", discogs_id=7101),
        Track(id=410, release_id=41, position=1, title="Live"),
        MusicFile(path="/music/Rundek/Held Bootleg/01.flac", track_id=410),
        Release(artist_id=1, title="Wikidata Bootleg", discogs_id=7102),
        Release(artist_id=1, title="Wanted Bootleg", discogs_id=7103,
                status=ReleaseStatus.WANTED),
        settings={"discogs_token": "tok"},
    ))

    result = run(discography.sync_artist(1))

    assert result == {"added": 2, "linked": 1, "merged": 1, "retracked": 2, "dated": 3,
                      "retitled": 4}
    releases, external = run(_shelf(1))
    assert releases == [
        ("Apsurdni Svijet", 9001, None, None, None, "1980-01-01", "album",
         "https://deezer/9001.jpg"),
        ("Brod U Boci", 9009, None, None, None, "2009-09-09", "album",
         "https://deezer/9009.jpg"),
        ("Held Bootleg", None, 7101, None, None, None, None, None),
        ("Nobody Credited", 9103, None, None, None, None, None, None),
        ("Plan B", 9006, None, None, None, None, None, None),
        ("Ruke", 9003, None, None, None, "2004-03-03", None, None),
        ("Still Ours", 9101, 7201, None, None, "1995", None, None),
        ("U Širokom Svijetu", 9002, 7002, None, None, "2002-05-05", None, None),
        ("Unknown Now", 9102, None, None, None, None, None, None),
        ("Wanted Bootleg", None, 7103, None, None, None, None, None),
        ("Wikidata Bootleg", None, 7102, None, "Q7102", None, None, None),
    ]
    assert external == []
    assert [a for a in sources.asked if a[0] != "deezer album"] == [
        ("retrack", ["Apsurdni Svijet", "Bootleg", "Brod U Boci", "Held Bootleg",
                     "Nobody Credited",
                     "Plan B", "Ruke", "Still Ours", "U Širokom Svijetu", "Unknown Now",
                     "Wanted Bootleg", "Wikidata Bootleg"]),
        ("wikidata", "Q500"),
    ]
    assert sorted({a[1] for a in sources.asked if a[0] == "deezer album"}) == [
        9001, 9002, 9003, 9004, 9005, 9006, 9007, 9008, 9009, 9100, 9101, 9102, 9103]
    assert discography._kept_strays == {40}
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "stray 'Plan B' (deezer 9006) holds files/downloads — kept, review manually" in warned

    run(discography.sync_artist(1))
    warned_again = [r.getMessage() for r in caplog.records if "review manually" in r.getMessage()]
    assert len(warned_again) == 1


def test_an_act_the_streaming_catalogues_barely_know_is_read_from_discogs(sources):
    albums, details = page(600, *((600 + n, title, f"{1995 + n}-01-01") for n, title in
                                  enumerate(["Sunce", "Mjesec", "Zvijezde", "Oblaci", "Kiša"])))
    sources.pages[600] = albums
    sources.albums.update(details)
    sources.discogs_artist = 4242
    sources.masters = [
        {"id": 1, "title": "Haustor", "year": 1981, "thumb": "https://discogs/1.jpg"},
        {"id": 2, "title": "Moja Prva Ljubav / Crni Žbir", "year": 1981},
        {"id": 3, "title": "Treći Svijet", "year": 1985, "thumb": ""},
        {"id": 4, "title": "Bolero", "year": 1985},
        {"id": 5, "title": "Ulica Jorgovana", "year": None},
        {"id": 6, "title": "Seven", "record_type": "single", "year": 1984},
        {"id": 7, "title": "Four Tracks", "year": 1986},
        {"id": 8, "title": "Empty", "year": 1987},
    ]
    sources.details = {
        1: {"year": 1981, "track_count": 10},
        3: DiscogsError("500"),
        5: {"year": 1988, "track_count": 5},
        7: {"year": None, "track_count": 4},
        8: {},
    }
    sources.spotify = [{"id": "sp-treci", "title": "Treći Svijet", "release_date": "1988",
                        "record_type": "album", "cover_url": "https://spotify/treci.jpg"}]
    run(_setup(
        Artist(id=3, name="Haustor", deezer_id=600, artist_type="group", begin_year=1979),
        ArtistExternalId(artist_id=3, source="spotify", external_id="sp-haustor"),
        Release(artist_id=3, title="Bolero", discogs_id=4, record_type="album",
                release_date="1985-06-01"),
        Release(artist_id=3, title="Discogs Only", discogs_id=99),
        settings={"discogs_token": "tok", "spotify_client_id": "id",
                  "spotify_client_secret": "secret"},
    ))

    result = run(discography.sync_artist(3))

    assert result == {"added": 12, "linked": 1, "merged": 0, "retracked": 2, "dated": 0,
                      "retitled": 0}
    releases, external = run(_shelf(3))
    assert releases == [
        ("Bolero", None, 4, None, None, "1985-06-01", "album", None),
        ("Discogs Only", None, 99, None, None, None, None, None),
        ("Empty", None, 8, None, None, "1987", None, None),
        ("Four Tracks", None, 7, None, None, "1986", "single", None),
        ("Haustor", None, 1, None, None, "1981", "album", "https://discogs/1.jpg"),
        ("Kiša", 604, None, None, None, "1999-01-01", "album", "https://deezer/604.jpg"),
        ("Mjesec", 601, None, None, None, "1996-01-01", "album", "https://deezer/601.jpg"),
        ("Moja Prva Ljubav / Crni Žbir", None, 2, None, None, "1981", "single", None),
        ("Oblaci", 603, None, None, None, "1998-01-01", "album", "https://deezer/603.jpg"),
        ("Seven", None, 6, None, None, "1984", "single", None),
        ("Sunce", 600, None, None, None, "1995-01-01", "album", "https://deezer/600.jpg"),
        ("Treći Svijet", None, 3, "sp-treci", None, "1985", None, None),
        ("Ulica Jorgovana", None, 5, None, None, "1988", "ep", None),
        ("Zvijezde", 602, None, None, None, "1997-01-01", "album", "https://deezer/602.jpg"),
    ]
    assert external == [("discogs", "4242"), ("spotify", "sp-haustor")]
    assert [a for a in sources.asked if a[0].startswith(("discogs", "spotify"))] == [
        ("discogs", "tok"), ("discogs search", "Haustor"), ("discogs masters", 4242),
        ("discogs master", 1), ("discogs master", 3), ("discogs master", 5),
        ("discogs master", 7), ("discogs master", 8),
        ("spotify", "id", "secret"), ("spotify albums", "sp-haustor"),
    ]
    assert sources.closed == ["deezer", "discogs", "spotify", "deezer"]
    assert len(sources.stored) == 1 and sources.stored[0][0] == 4242
    assert [(m["id"], m.get("record_type"), m["year"]) for m in sources.stored[0][1]] == [
        (1, "album", 1981), (2, None, 1981), (3, None, 1985), (4, None, 1985),
        (5, "ep", 1988), (6, "single", 1984), (7, "single", 1986), (8, None, 1987)]


@pytest.mark.parametrize("spotify", [SpotifyCooldown("resting"), SpotifyError("403")])
def test_a_broken_rung_costs_only_its_own_candidates(sources, spotify):
    sources.masters = DiscogsError("404 for the Various pseudo-artist")
    sources.spotify = spotify
    run(_setup(
        Artist(id=4, name="Buco", begin_year=1990),
        ArtistExternalId(artist_id=4, source="discogs", external_id="194"),
        ArtistExternalId(artist_id=4, source="spotify", external_id="sp-buco"),
        Release(artist_id=4, title="Discogs Only", discogs_id=99),
        settings={"discogs_token": "tok", "spotify_client_id": "id",
                  "spotify_client_secret": "secret"},
    ))

    result = run(discography.sync_artist(4))

    assert result == {"added": 0, "linked": 0, "merged": 0, "retracked": 2, "dated": 0,
                      "retitled": 0}
    releases, external = run(_shelf(4))
    assert releases == [("Discogs Only", None, 99, None, None, None, None, None)]
    assert external == [("discogs", "194"), ("spotify", "sp-buco")]
    assert sources.asked == [
        ("discogs", "tok"), ("discogs masters", 194),
        ("spotify", "id", "secret"), ("spotify albums", "sp-buco"),
        ("retrack", ["Discogs Only"]), ("wikidata", None),
    ]
    assert sources.stored == []
    assert sources.closed == ["discogs", "spotify"]


def test_nothing_configured_asks_only_deezer_and_an_unknown_artist_is_refused(sources):
    sources.pages[700] = []
    run(_setup(Artist(id=5, name="Quiet", deezer_id=700),
               ArtistExternalId(artist_id=5, source="spotify", external_id="sp-quiet"),
               Release(artist_id=5, title="Discogs Only", discogs_id=99)))

    assert run(discography.sync_artist(5)) == {
        "added": 0, "linked": 0, "merged": 0, "retracked": 2, "dated": 0, "retitled": 0}
    assert run(_shelf(5))[0] == []
    assert sources.asked == [("retrack", ["Discogs Only"]), ("wikidata", None)]
    assert sources.closed == ["deezer", "deezer"]
    with pytest.raises(DiscographyError, match="artist 999 not found"):
        run(discography.sync_artist(999))


def test_discogs_is_asked_only_what_it_has_not_already_answered(sources):
    sources.masters = [
        {"id": 1, "title": "Prva / Druga", "year": 1970},
        {"id": 2, "title": "Undated", "record_type": "album", "year": None},
        {"id": 3, "title": "Known", "year": 1972, "thumb": "https://discogs/3.jpg"},
    ]
    run(_setup(
        Artist(id=6, name="Typed Already"),
        Artist(id=7, name="Nowhere On Discogs"),
        ArtistExternalId(artist_id=6, source="discogs", external_id="77"),
        Release(artist_id=6, title="Known", discogs_id=3, record_type="ep"),
        settings={"discogs_token": "tok", "spotify_client_id": "id",
                  "spotify_client_secret": "secret"},
    ))

    assert run(discography.sync_artist(6)) == {
        "added": 2, "linked": 0, "merged": 0, "retracked": 2, "dated": 0, "retitled": 0}
    assert run(_shelf(6)) == ([
        ("Known", None, 3, None, None, "1972", "ep", None),
        ("Prva / Druga", None, 1, None, None, "1970", "single", None),
        ("Undated", None, 2, None, None, None, "album", None),
    ], [("discogs", "77")])
    assert sources.asked == [("discogs", "tok"), ("discogs masters", 77),
                             ("retrack", ["Known", "Prva / Druga", "Undated"]),
                             ("wikidata", None)]
    assert sources.stored == []
    assert sources.closed == ["discogs"]

    sources.asked.clear()
    sources.closed.clear()
    assert run(discography.sync_artist(7)) == {
        "added": 0, "linked": 0, "merged": 0, "retracked": 2, "dated": 0, "retitled": 0}
    assert run(_shelf(7)) == ([], [])
    assert sources.asked == [("discogs", "tok"), ("discogs search", "Nowhere On Discogs"),
                             ("retrack", []), ("wikidata", None)]
    assert sources.closed == ["discogs"]
