from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import Artist, Release, ReleaseStatus, Track
from opus.music.metadata.deezer import DeezerNotFound
from opus.music.pipeline import records


def test_a_record_deezer_no_longer_has_is_asked_about_once(clean, monkeypatch):
    asked: list[int] = []

    class Deezer:
        async def get_album(self, album_id):
            asked.append(album_id)
            raise DeezerNotFound(f"Deezer has no data on /album/{album_id}")

        async def close(self):
            pass

    class Wikidata:
        async def close(self):
            pass

    async def described(release, client):
        release.description = release.description or ""

    monkeypatch.setattr(records, "DeezerClient", Deezer)
    monkeypatch.setattr(records.wd, "WikidataClient", Wikidata)
    monkeypatch.setattr(records.tracklists, "ensure_description", described)
    monkeypatch.setattr(records, "RECORDS_PACE", 0)

    async def setup():
        async with db.SessionLocal() as session:
            session.add(Artist(id=1, name="Black Sabbath"))
            session.add(Release(id=10, artist_id=1, title="Heaven and Hell", deezer_id=15483670,
                                description="", status=ReleaseStatus.COMPLETE))
            session.add(Track(release_id=10, position=1, title="Neon Knights", deezer_id=5))
            await session.commit()

    run(setup())
    assert run(records._records_pass()) == 0
    assert run(records._records_pass()) == 0
    assert asked == [15483670]

    async def links():
        async with db.SessionLocal() as session:
            release = await session.get(Release, 10)
            tracks = (await session.execute(select(Track.deezer_id))).scalars().all()
            return release.deezer_id, tracks

    assert run(links()) == (None, [None])
