from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import Artist, MusicFile, Release, ReleaseStatus, Track
from opus.music.metadata.dedupe import _merge_legacy_duplicates


async def _seed(*rows):
    async with db.SessionLocal() as session:
        session.add(Artist(id=1, name="AC/DC"))
        await session.flush()
        for row in rows:
            session.add(row)
            await session.flush()
        await session.commit()


async def _merge():
    async with db.SessionLocal() as session:
        releases = list((await session.execute(
            select(Release).where(Release.artist_id == 1).order_by(Release.id))).scalars())
        merged = await _merge_legacy_duplicates(session, releases)
        await session.commit()
    async with db.SessionLocal() as session:
        shelf = {r.id: (r.title, r.deezer_id, r.discogs_id, r.spotify_id, r.wikidata_id,
                        r.release_date, r.record_type)
                 for r in (await session.execute(select(Release))).scalars()}
    return merged, shelf


def _held(release_id: int, path: str):
    return (Track(id=release_id * 10, release_id=release_id, position=1, title="One"),
            MusicFile(path=path, track_id=release_id * 10))


def test_a_discogs_only_row_folds_into_its_deezer_twin_and_a_ghost_into_the_files(clean):
    run(_seed(
        Release(id=1, artist_id=1, title="High Voltage", discogs_id=10, release_date="1976",
                record_type="album"),
        Release(id=2, artist_id=1, title="High Voltage", deezer_id=100, release_date="1980-05-01"),
        Release(id=3, artist_id=1, title="Powerage", discogs_id=11, track_count=10),
        Release(id=4, artist_id=1, title="Powerage", deezer_id=101, track_count=9),
        Release(id=5, artist_id=1, title="The Album", discogs_id=12, release_date="1977"),
        Release(id=6, artist_id=1, title="The Albums", deezer_id=102, release_date="2008"),
        Release(id=7, artist_id=1, title="Dirty Deeds", discogs_id=13),
        Release(id=8, artist_id=1, title="Dirty Deeds", deezer_id=103, discogs_id=14),
        Release(id=9, artist_id=1, title="Flick of the Switch", discogs_id=15,
                status=ReleaseStatus.WANTED),
        Release(id=10, artist_id=1, title="Flick of the Switch", deezer_id=104,
                status=ReleaseStatus.WANTED),
        Release(id=11, artist_id=1, title="Back in Black", discogs_id=16, release_date="1980"),
        *_held(11, "/music/ACDC/Back in Black/01.flac"),
        Release(id=12, artist_id=1, title="Back in Black", deezer_id=105, spotify_id="sp5",
                wikidata_id="Q105"),
        Release(id=13, artist_id=1, title="Blow Up Your Video", spotify_id="sp6"),
        Release(id=14, artist_id=1, title="Blow Up Your Video", deezer_id=106),
    ))

    merged, shelf = run(_merge())

    assert merged == 2
    # the Discogs row is gone, its id and its earlier year live on in the Deezer one
    assert 1 not in shelf
    assert shelf[2] == ("High Voltage", 100, 10, None, None, "1976", "album")
    # a different tracklist, a plural without a year to back it, a twin that
    # already has a Discogs id, and an album somebody is fetching all stay
    assert {3, 4, 5, 6, 7, 8, 9, 10} <= shelf.keys()
    # the file-less Deezer row dissolves into the row holding the files, and
    # hands it every id it had
    assert 12 not in shelf
    assert shelf[11] == ("Back in Black", 105, 16, "sp5", "Q105", "1980", None)
    # a ghost with no file-holding twin has nowhere to dissolve into
    assert {13, 14} <= shelf.keys()


def test_nothing_to_merge_merges_nothing(clean):
    run(_seed(Release(id=1, artist_id=1, title="Rock or Bust", deezer_id=100)))
    assert run(_merge()) == (0, {1: ("Rock or Bust", 100, None, None, None, None, None)})
