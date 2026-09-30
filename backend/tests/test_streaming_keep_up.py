import asyncio
import datetime

import pytest
from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import AwardTitle, Streaming
from opus.settings_store import RuntimeConfig
from opus.video import streaming
from opus.video.metadata import tmdb

NETFLIX = {"id": 8, "name": "Netflix", "logo": None}
MAX = {"id": 1899, "name": "Max", "logo": None}
SKY = {"id": 29, "name": "Sky", "logo": None}

CONFIG = RuntimeConfig({"streaming_subscriptions": "8, 1899,x"})


def ago(delta: datetime.timedelta) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - delta


def recent(moment: datetime.datetime) -> bool:
    return moment > ago(datetime.timedelta(minutes=1))


@pytest.fixture
def lookups(clean, monkeypatch):
    asked: list[tuple[str, int]] = []
    answers: dict[tuple[str, int], list[dict] | Exception] = {}

    async def lookup(config, media_type, tmdb_id):
        asked.append((media_type, tmdb_id))
        answer = answers.get((media_type, tmdb_id), [])
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(tmdb, "streaming", lookup)
    return asked, answers


async def _known(media_type, tmdb_id, services, *, checked, asked=datetime.timedelta(0)):
    async with db.SessionLocal() as session:
        session.add(Streaming(media_type=media_type, tmdb_id=tmdb_id, services=services,
                              checked_at=ago(checked), asked_at=ago(asked)))
        await session.commit()


async def _award(media_type, tmdb_id):
    async with db.SessionLocal() as session:
        session.add(AwardTitle(media_type=media_type, tmdb_id=tmdb_id, title=f"Winner {tmdb_id}"))
        await session.commit()


async def _rows():
    async with db.SessionLocal() as session:
        rows = (await session.scalars(
            select(Streaming).order_by(Streaming.media_type, Streaming.tmdb_id))).all()
        return {(r.media_type, r.tmdb_id): r for r in rows}


def attached(cards, kind=None):
    async def go():
        async with db.SessionLocal() as session:
            return await streaming.attach(session, CONFIG, cards, kind)

    return run(go())


def kept_up():
    async def go():
        async with db.SessionLocal() as session:
            return await streaming.keep_up(session, CONFIG)

    return run(go())


def test_the_house_pays_for_the_services_its_setting_names():
    assert streaming.subscribed(CONFIG) == {8, 1899}
    assert streaming.subscribed(RuntimeConfig({"streaming_subscriptions": ""})) == set()


def test_a_known_title_is_served_from_the_table_whatever_its_age(lookups):
    asked, _ = lookups
    old = datetime.timedelta(days=3)
    run(_known("movie", 1, [NETFLIX, SKY], checked=old, asked=datetime.timedelta(days=10)))

    cards = attached([{"media_type": "movie", "tmdb_id": 1}])
    assert cards[0]["streaming"] == [{**NETFLIX, "ours": True}, {**SKY, "ours": False}]
    assert asked == []
    row = run(_rows())[("movie", 1)]
    assert recent(row.asked_at) and not recent(row.checked_at)


def test_a_title_never_looked_up_is_asked_once_and_kept(lookups):
    asked, answers = lookups
    answers[("tv", 5)] = [MAX]

    cards = attached([{"tmdb_id": 5}, {"tmdb_id": 5, "title": "again"}, {"tmdb_id": None}],
                     kind="tv")
    assert asked == [("tv", 5)]
    assert cards[0]["streaming"] == cards[1]["streaming"] == [{**MAX, "ours": True}]
    assert "streaming" not in cards[2]
    row = run(_rows())[("tv", 5)]
    assert row.services == [MAX] and recent(row.checked_at) and recent(row.asked_at)


def test_a_card_that_is_not_a_film_or_a_series_is_left_alone(lookups):
    asked, _ = lookups
    cards = [{"media_type": "person", "tmdb_id": 3}, {"media_type": "movie"}]
    assert attached(cards) == [{"media_type": "person", "tmdb_id": 3}, {"media_type": "movie"}]
    assert asked == []


def test_a_title_known_to_stream_nowhere_answers_an_empty_list(lookups):
    run(_known("movie", 2, [], checked=datetime.timedelta(0)))
    assert attached([{"media_type": "movie", "tmdb_id": 2}])[0]["streaming"] == []


def test_a_slow_lookup_leaves_the_card_unknown_and_lands_behind_the_answer(clean, monkeypatch):
    monkeypatch.setattr(streaming, "WAIT_S", 0.01)

    async def slow(config, media_type, tmdb_id):
        await asyncio.sleep(0.2)
        return [NETFLIX]

    monkeypatch.setattr(tmdb, "streaming", slow)

    async def go():
        async with db.SessionLocal() as session:
            cards = await streaming.attach(session, CONFIG, [{"media_type": "movie", "tmdb_id": 9}])
        await asyncio.gather(*streaming._behind)
        return cards

    assert run(go()) == [{"media_type": "movie", "tmdb_id": 9}]
    assert run(_rows())[("movie", 9)].services == [NETFLIX]


def test_keeping_up_asks_the_stale_rows_and_the_award_titles_never_looked_up(lookups):
    asked, answers = lookups
    fresh, stale = datetime.timedelta(hours=2), datetime.timedelta(days=2)
    run(_known("movie", 1, [NETFLIX], checked=fresh))
    run(_known("movie", 2, [NETFLIX], checked=stale, asked=datetime.timedelta(days=5)))
    run(_award("movie", 3))
    run(_award("tv", 4))
    run(_known("tv", 4, [SKY], checked=fresh))
    answers[("movie", 2)] = [MAX]
    answers[("movie", 3)] = [SKY]

    assert kept_up() == 2
    assert sorted(asked) == [("movie", 2), ("movie", 3)]
    rows = run(_rows())
    assert rows[("movie", 2)].services == [MAX] and recent(rows[("movie", 2)].checked_at)
    assert not recent(rows[("movie", 2)].asked_at)
    assert rows[("movie", 3)].services == [SKY] and recent(rows[("movie", 3)].asked_at)
    assert rows[("movie", 1)].services == [NETFLIX] and not recent(rows[("movie", 1)].checked_at)


def test_keeping_up_forgets_what_nobody_asked_for_in_a_month_unless_it_won(lookups):
    asked, _ = lookups
    month = streaming.FORGOTTEN + datetime.timedelta(days=1)
    run(_known("movie", 1, [NETFLIX], checked=datetime.timedelta(0), asked=month))
    run(_known("movie", 2, [NETFLIX], checked=datetime.timedelta(0), asked=month))
    run(_award("movie", 2))

    assert kept_up() == 0
    assert asked == []
    assert list(run(_rows())) == [("movie", 2)]


def test_a_failed_refresh_keeps_what_was_known_and_is_asked_again(lookups):
    asked, answers = lookups
    stale = datetime.timedelta(days=2)
    run(_known("movie", 1, [NETFLIX], checked=stale))
    run(_known("movie", 2, [NETFLIX], checked=stale))
    answers[("movie", 1)] = tmdb.TmdbError("TMDB /movie/1/watch/providers answered 429", 429)
    answers[("movie", 2)] = [SKY]

    assert kept_up() == 2
    rows = run(_rows())
    assert rows[("movie", 1)].services == [NETFLIX] and not recent(rows[("movie", 1)].checked_at)
    assert rows[("movie", 2)].services == [SKY]

    asked.clear()
    answers[("movie", 1)] = [MAX]
    assert kept_up() == 1
    assert asked == [("movie", 1)]
    assert run(_rows())[("movie", 1)].services == [MAX]
