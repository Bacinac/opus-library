from conftest import run
from opus import db
from opus.video import streaming
from opus.video.metadata import tmdb


class Config:
    def get(self, key):
        return {"streaming_subscriptions": "8"}.get(key, "")


def test_a_failed_lookup_leaves_the_card_unknown_and_the_rest_answered(clean, monkeypatch):
    async def lookup(config, media_type, tmdb_id):
        if tmdb_id == 2:
            raise tmdb.TmdbError("TMDB /movie/2/watch/providers answered 429", 429)
        return [{"id": 8, "name": "Netflix", "logo": None}]

    monkeypatch.setattr(tmdb, "streaming", lookup)

    async def go():
        async with db.SessionLocal() as session:
            return await streaming.attach(session, Config(), [
                {"media_type": "movie", "tmdb_id": 1}, {"media_type": "movie", "tmdb_id": 2}])

    cards = run(go())
    assert cards[0]["streaming"] == [{"id": 8, "name": "Netflix", "logo": None, "ours": True}]
    assert "streaming" not in cards[1]


def test_the_refresh_keeps_what_answered(clean, monkeypatch):
    async def lookup(config, media_type, tmdb_id):
        if tmdb_id == 2:
            raise tmdb.TmdbError("TMDB unreachable")
        return []

    monkeypatch.setattr(tmdb, "streaming", lookup)
    found = run(streaming._fetch(Config(), [("movie", 1), ("movie", 2)], {}))
    assert found == {("movie", 1): []}
