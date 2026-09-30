import datetime

import pytest

from conftest import library, run, signed_in
from opus import db
from opus.models import Artist, Release


@pytest.fixture
def followed(clean):
    today = datetime.date.today()

    async def build():
        async with db.SessionLocal() as session:
            session.add_all([
                Artist(id=1, name="Followed", monitored=True),
                Artist(id=2, name="Not followed", monitored=False),
            ])
            await session.flush()
            session.add_all([
                Release(id=1, artist_id=1, title="This month",
                        release_date=(today - datetime.timedelta(days=10)).isoformat()),
                Release(id=2, artist_id=1, title="Last year",
                        release_date=(today - datetime.timedelta(days=400)).isoformat()),
                Release(id=4, artist_id=1, title="Announced",
                        release_date=(today + datetime.timedelta(days=30)).isoformat()),
                Release(id=3, artist_id=2, title="Somebody else's",
                        release_date=(today - datetime.timedelta(days=5)).isoformat()),
            ])
            await session.commit()

    run(build())


def test_news_is_what_the_followed_put_out_lately_held_or_not(followed):
    async def scenario():
        async with library(await signed_in("boss")) as client:
            return await client.get("/api/music/releases", params={"fresh": 90})

    answer = run(scenario())
    assert answer.status_code == 200, answer.text
    assert [(c["title"], c["held"]) for c in answer.json()] == [("This Month", 0)]
