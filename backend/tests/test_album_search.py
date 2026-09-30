from sqlalchemy import select

from conftest import _empty, run
from opus import db
from opus.models import Artist, MusicFile, Release, ReleaseVariant, Track
from opus.music.library import albumsearch
from opus.music.metadata.discogs import DiscogsError
from opus.music.metadata.spotify import SpotifyCooldown

FILES = ["Rain", "Wind", "Snow", "Hail"]


def tracks(*titles):
    return [{"title": t, "position": n, "duration_sec": 100 + n}
            for n, t in enumerate(titles, 1)]


class Source:
    """A stand-in for a catalogue: every answer given by name, every question written down."""

    def __init__(self, **answers):
        self.answers = answers
        self.asked: list[tuple] = []

    def __getattr__(self, name):
        async def answer(*args):
            self.asked.append((name, *[a for a in args if not hasattr(a, "execute")]))
            given = self.answers.get(name)
            value = given(*[a for a in args if not hasattr(a, "execute")]) if callable(given) else given
            if isinstance(value, Exception):
                raise value
            return value if value is not None else []
        return answer


async def _folder(extra_releases=()) -> tuple[Artist, list[MusicFile]]:
    async with db.SessionLocal() as session:
        artist = Artist(name="The Weather")
        session.add(artist)
        await session.flush()
        rows = [MusicFile(path=f"/music/The Weather/Seasons/{n:02d} {t}.flac", tag_title=t,
                          tag_track=n) for n, t in enumerate(FILES, 1)]
        session.add_all(rows)
        for make in extra_releases:
            await make(session, artist)
        await session.commit()
        return artist, rows


async def _hunt(catalog, discogs=None, spotify=None, external=None, best_so_far=0,
                extra_releases=()):
    artist, rows = await _folder(extra_releases)
    async with db.SessionLocal() as session:
        artist = await session.get(Artist, artist.id)
        rows = [await session.get(MusicFile, r.id) for r in rows]
        found = await albumsearch.album_search_fallback(
            session, catalog, discogs, spotify, artist, external or {},
            "The Weather", "Seasons", FILES[:], rows, len(rows), best_so_far)
        await session.commit()
        shelf = sorted(
            ((r.title, r.deezer_id, r.discogs_id, r.spotify_id, r.artist_id == artist.id)
             for r in (await session.execute(select(Release))).scalars()), key=repr)
        winner = None
        if found is not None:
            listed = (await session.execute(
                select(Track.position, Track.title).where(Track.release_id == found.id)
                .order_by(Track.position))).all()
            winner = (found.title, found.deezer_id, found.discogs_id,
                      found.spotify_id, found.record_type, found.release_date, found.track_count,
                      [tuple(t) for t in listed])
        variants = sorted((v.variant, v.source, v.external_id)
                          for v in (await session.execute(select(ReleaseVariant))).scalars())
        return winner, shelf, variants


def test_a_perfect_catalogue_album_stops_the_hunt(clean):
    async def held(session, artist):
        session.add(Release(artist_id=artist.id, title="Seasons", deezer_id=5))

    catalog = Source(
        search_albums=lambda q: [
            {"id": 70, "title": "Seasons", "artist": {"name": "The Weather"}, "source": "deezer",
             "mixes": ["atmos"]},
            {"id": 71, "title": "Seasons", "artist": {"name": "The Weather"}, "source": "deezer"},
        ],
        get_album_tracks=lambda album_id, source: tracks(*FILES),
        get_album={"title": "Seasons (Remastered)", "record_type": "album",
                   "cover_medium": "https://cover", "release_date": "1999-01-01"},
    )
    discogs = Source()
    winner, shelf, variants = run(_hunt(catalog, discogs, extra_releases=[held]))
    assert winner == ("Seasons (Remastered)", 71, None, None, "album", "1999-01-01", None, [])
    assert [a[0] for a in catalog.asked] == ["search_albums", "get_album_tracks", "get_album"]
    assert discogs.asked == []
    assert variants == [("atmos", "deezer", "70")]
    assert len(shelf) == 2


def test_songs_name_the_album_when_the_title_does_not(clean):
    catalog = Source(
        search_albums=[],
        search_tracks=lambda q: [{"artist": {"name": "The Weather"},
                                  "album": {"id": 90, "title": "Four Seasons"}, "source": "deezer"}],
        get_album_tracks=lambda album_id, source: tracks(*FILES),
        get_album=RuntimeError("deezer is down"),
    )
    winner, _, _ = run(_hunt(catalog))
    assert winner == ("Four Seasons", 90, None, None, None, None, None, [])
    assert [a[0] for a in catalog.asked].count("search_tracks") == 3


def test_a_partial_elsewhere_never_wins_under_another_name(clean):
    catalog = Source(
        search_albums=[],
        search_tracks=lambda q: [{"artist": {"name": "The Weather"},
                                  "album": {"id": 91, "title": "Jazz Hits"}, "source": "deezer"}],
        get_album_tracks=lambda album_id, source: tracks("Rain", "Wind", "Snow", "Other"),
    )
    winner, shelf, _ = run(_hunt(catalog))
    assert winner is None and shelf == []


def test_a_fuller_discogs_version_is_materialised(clean):
    catalog = Source()
    discogs = Source(
        cached_artist_masters=lambda discogs_id: [{"id": 500, "title": "Seasons", "year": 1990},
                                                  {"id": 501, "title": "Other Things"}],
        master_tracklist=lambda master_id: tracks("Rain", "Wind", "Snow"),
        master_versions=lambda master_id: [
            {"id": 601, "format": "Vinyl, LP", "year": "1990"},
            {"id": 602, "format": "CD, Album, Reissue", "year": "2005"},
            {"id": 603, "format": "SACD, Multichannel", "year": "2003"},
        ],
        release_tracklist=lambda release_id: (tracks(*FILES) if release_id == 602
                                              else DiscogsError("not this one")),
    )
    winner, shelf, variants = run(_hunt(catalog, discogs, external={"discogs": "42"}))
    assert winner == ("Seasons", None, 500, None, None, "1990", 4, [
        (1, "Rain"), (2, "Wind"), (3, "Snow"), (4, "Hail")])
    assert ("master_tracklist", 501) not in discogs.asked
    assert [a for a in discogs.asked if a[0] == "release_tracklist"] == [
        ("release_tracklist", 602)]
    assert variants == []


def test_a_fuller_edition_replaces_the_tracklist_of_its_own_row(clean):
    async def held(session, artist):
        release = Release(artist_id=artist.id, title="Seasons", discogs_id=500)
        session.add(release)
        await session.flush()
        session.add_all([Track(release_id=release.id, position=n, title=t)
                         for n, t in enumerate(["Rain", "Wind", "Snow"], 1)])

    discogs = Source(
        search_masters=lambda artist, album: [{"id": 500, "title": "Seasons"}],
        master_tracklist=lambda master_id: tracks("Rain", "Wind", "Snow"),
        master_versions=lambda master_id: [{"id": 602, "format": "CD", "year": "2005"}],
        release_tracklist=lambda release_id: tracks(*FILES),
    )
    winner, shelf, _ = run(_hunt(Source(), discogs, extra_releases=[held]))
    assert winner[2] == 500 and winner[7] == [(1, "Rain"), (2, "Wind"), (3, "Snow"), (4, "Hail")]
    assert shelf == [("Seasons", None, 500, None, True)]


def test_a_master_held_by_another_folder_is_freed_for_this_one(clean):
    async def held(session, artist):
        release = Release(artist_id=artist.id, title="Seasons", discogs_id=500, track_count=2)
        session.add(release)
        await session.flush()
        track = Track(release_id=release.id, position=1, title="Elsewhere")
        session.add(track)
        await session.flush()
        session.add(MusicFile(path="/music/The Weather/Other/01.flac", track_id=track.id))

    discogs = Source(
        search_masters=lambda artist, album: [{"id": 500, "title": "Seasons", "year": 1990,
                                               "thumb": "https://thumb"}],
        master_tracklist=lambda master_id: tracks(*FILES),
    )
    winner, shelf, _ = run(_hunt(Source(), discogs, extra_releases=[held]))
    assert winner == ("Seasons", None, 500, None, None, "1990", None, [])
    assert shelf == [("Seasons", None, 500, None, True),
                     ("Seasons", None, None, None, True)]


def test_a_release_without_a_master_is_found_by_release_search(clean):
    discogs = Source(
        search_masters=DiscogsError("search refused"),
        search_releases=lambda artist, album: [{"id": 777, "title": "Seasons"}],
        release_tracklist=lambda release_id: tracks(*FILES),
    )
    winner, _, _ = run(_hunt(Source(), discogs))
    assert winner == ("Seasons", None, 777, None, None, None, 4, [
        (1, "Rain"), (2, "Wind"), (3, "Snow"), (4, "Hail")])


def test_spotify_is_the_last_word(clean):
    spotify = Source(
        search_albums=lambda artist, album: [
            {"id": "sp1", "title": "Seasons", "release_date": "2001-02-03",
             "record_type": "album", "cover_url": "https://sp"}],
        album_tracks=lambda album_id: tracks(*FILES[:3]),
    )
    winner, _, _ = run(_hunt(Source(), Source(search_masters=[], search_releases=[]), spotify))
    assert winner == ("Seasons", None, None, "sp1", "album", "2001-02-03", None, [])


def test_a_cooling_spotify_and_a_box_set_leave_nothing(clean):
    spotify = Source(artist_albums=SpotifyCooldown("wait"))
    catalog = Source(
        search_albums=lambda q: [{"id": 80, "title": "Seasons", "artist": {"name": "The Weather"},
                                  "source": "deezer"}],
        get_album_tracks=lambda album_id, source: tracks(*FILES, *[f"Extra {n}" for n in range(20)]),
    )
    winner, shelf, _ = run(_hunt(catalog, None, spotify, external={"spotify": "abc"}))
    assert winner is None and shelf == []
    assert spotify.asked == [("artist_albums", "abc")]


def test_only_more_files_or_the_same_files_complete_beat_what_is_held(clean):
    def catalog(*titles):
        return Source(
            search_albums=lambda q: [{"id": 81, "title": "Seasons",
                                      "artist": {"name": "The Weather"}, "source": "deezer"}],
            get_album_tracks=lambda album_id, source: tracks(*titles),
            get_album={},
        )

    winner, _, _ = run(_hunt(catalog("Rain", "Wind", "Snow"), best_so_far=3))
    assert winner == ("Seasons", 81, None, None, None, None, None, [])


def test_fewer_files_than_are_held_never_win(clean):
    catalog = Source(
        search_albums=lambda q: [{"id": 82, "title": "Seasons", "artist": {"name": "The Weather"},
                                  "source": "deezer"}],
        get_album_tracks=lambda album_id, source: tracks("Rain", "Wind"),
    )
    winner, _, _ = run(_hunt(catalog, best_so_far=3))
    assert winner is None


def test_a_winner_already_on_the_shelf_is_reused_only_for_its_own_artist(clean):
    async def elsewhere(session, artist):
        other = Artist(name="Somebody Else")
        session.add(other)
        await session.flush()
        session.add(Release(artist_id=other.id, title="Seasons", deezer_id=71))
        session.add(Release(artist_id=other.id, title="Seasons", discogs_id=300))

    async def ours(session, artist):
        session.add(Release(artist_id=artist.id, title="Seasons", discogs_id=400, track_count=4))
        session.add(Release(artist_id=artist.id, title="Seasons", spotify_id="sp9"))

    deezer = Source(
        search_albums=lambda q: [{"id": 71, "title": "Seasons", "artist": {"name": "The Weather"},
                                  "source": "deezer"}],
        get_album_tracks=lambda album_id, source: tracks(*FILES), get_album={})
    assert run(_hunt(deezer, extra_releases=[elsewhere]))[0] is None

    def discogs(master_id):
        return Source(search_masters=lambda artist, album: [{"id": master_id, "title": "Seasons"}],
                      master_tracklist=lambda m: tracks(*FILES))

    run(_empty())
    assert run(_hunt(Source(), discogs(300), extra_releases=[elsewhere]))[0] is None
    run(_empty())
    winner, shelf, _ = run(_hunt(Source(), discogs(400), extra_releases=[ours]))
    assert winner[2] == 400 and len(shelf) == 2
    run(_empty())
    spotify = Source(search_albums=lambda artist, album: [{"id": "sp9", "title": "Seasons"}],
                     album_tracks=lambda album_id: tracks(*FILES))
    winner, shelf, _ = run(_hunt(Source(), Source(), spotify, extra_releases=[ours]))
    assert winner[3] == "sp9" and len(shelf) == 2
