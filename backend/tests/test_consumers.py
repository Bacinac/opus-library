import opus_auth
import pytest
from sqlalchemy.dialects.postgresql import insert
from starlette.routing import Match

from conftest import library, run, signed_in
from opus import auth, db, settings_store
from opus.main import app
from opus.models import Setting
from opus.settings_store import RuntimeConfig

TOKENS = {"player": "player-token-0123456789", "downloads": "downloads-token-0123456789",
          "cameras": "cameras-token-0123456789"}
RUNTIME = RuntimeConfig({auth.token_key(name): token for name, token in TOKENS.items()})
ROSTER = {"boss": (1, "admin", False)}
CHECKSUM = "0123456789abcdef0123456789abcdef01234567"

PLAYER = [
    ("GET", "/api/music/search"),
    ("GET", "/api/auth/people"),
    ("POST", "/api/auth/verify"),
    ("POST", "/api/auth/passkey/verify"),
    ("POST", "/api/auth/devices/ask"),
    ("POST", "/api/auth/devices/claim"),
    ("POST", "/api/auth/devices/verify"),
    ("GET", "/api/storage"),
    ("GET", "/api/video/movies"),
    ("GET", "/api/video/movies/12"),
    ("GET", "/api/video/movies/12/playback"),
    ("GET", "/api/video/series"),
    ("GET", "/api/video/series/12"),
    ("GET", "/api/video/episodes"),
    ("GET", "/api/video/episodes/12/playback"),
    ("GET", "/api/video/discover/popular"),
    ("GET", "/api/video/discover/upcoming"),
    ("GET", "/api/video/discover/trending"),
    ("GET", "/api/video/discover/genres"),
    ("GET", "/api/video/discover/browse"),
    ("GET", "/api/video/discover/awarded"),
    ("GET", "/api/video/discover/held/people"),
    ("GET", "/api/video/discover/held/studios"),
    ("GET", "/api/video/discover/company/12"),
    ("GET", "/api/video/discover/person/12"),
    ("GET", "/api/video/discover/tv/12"),
    ("GET", "/api/video/discover/tv/12/season/1"),
    ("GET", "/api/video/search/movies"),
    ("GET", "/api/video/search/series"),
    ("POST", "/api/video/movies"),
    ("POST", "/api/video/movies/12/search"),
    ("POST", "/api/video/series"),
    ("PATCH", "/api/video/series/12/seasons/1"),
    ("POST", "/api/video/series/12/seasons/1/search"),
    ("POST", "/api/video/episodes/12/search"),
    ("GET", "/api/music/tracks"),
    ("GET", "/api/music/tracks/12/playback"),
    ("GET", "/api/music/tracks/12/lyrics"),
    ("GET", "/api/music/releases/12/lyrics"),
    ("GET", "/api/music/releases/12/playback"),
    ("GET", "/api/music/releases/12/about"),
    ("POST", "/api/music/releases/12/download"),
    ("GET", "/api/music/artists"),
    ("GET", "/api/music/artists/12"),
    ("POST", "/api/music/artists"),
    ("GET", "/api/music/search/library"),
    ("GET", "/api/music/search/artists"),
    ("GET", "/api/music/library/stats"),
    ("GET", "/api/photos/timeline/buckets"),
    ("GET", "/api/photos/timeline"),
    ("GET", "/api/photos/onthisday"),
    ("GET", "/api/photos/years"),
    ("GET", "/api/photos/people"),
    ("GET", "/api/photos/places"),
    ("GET", "/api/photos/draw"),
    ("GET", f"/api/photos/{CHECKSUM}"),
    ("GET", f"/api/photos/{CHECKSUM}/play"),
    ("GET", f"/api/photos/{CHECKSUM}/playback"),
    ("GET", f"/api/photos/{CHECKSUM}/tile"),
    ("GET", f"/api/photos/{CHECKSUM}/preview"),
    ("GET", "/api/photos/faces/12/crop"),
    ("GET", "/api/photos/faces/12/portrait"),
    ("GET", "/api/photos/people/12/morph"),
]

DOWNLOADS = [
    ("GET", "/api/auth/people"),
    ("POST", "/api/auth/verify"),
    ("POST", "/api/auth/passkey/verify"),
]

CAMERAS = [
    ("GET", "/api/auth/people"),
    ("GET", "/api/photos/people"),
    ("GET", "/api/photos/people/12/faces"),
    ("GET", f"/api/photos/{CHECKSUM}/preview"),
    ("GET", "/api/photos/faces/12/crop"),
]

CALLS = {"player": PLAYER, "downloads": DOWNLOADS, "cameras": CAMERAS}
EVERY_CALL = sorted(set(PLAYER + DOWNLOADS + CAMERAS))

REFUSED = [
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("GET", "/api/auth/token"),
    ("POST", "/api/auth/token"),
    ("POST", "/api/auth/people"),
    ("PATCH", "/api/auth/people/filip"),
    ("DELETE", "/api/auth/people/filip"),
    ("POST", "/api/auth/password"),
    ("GET", "/api/auth/devices"),
    ("POST", "/api/auth/devices"),
    ("PATCH", "/api/auth/devices/3"),
    ("DELETE", "/api/auth/devices/3"),
    ("DELETE", f"/api/photos/{CHECKSUM}"),
    ("PUT", f"/api/photos/{CHECKSUM}/place"),
    ("POST", f"/api/photos/{CHECKSUM}/turn"),
    ("GET", "/api/photos/vault"),
    ("POST", "/api/photos/offer"),
    ("POST", "/api/photos/contacts/sync"),
    ("GET", "/api/photos/contacts"),
    ("PUT", "/api/photos/clusters/4/person"),
    ("POST", "/api/photos/faces/discard"),
    ("DELETE", "/api/photos/library/focus"),
    ("POST", "/api/photos/library/scan"),
    ("GET", "/api/downloads"),
    ("GET", "/api/links"),
    ("POST", "/api/links/example"),
    ("POST", "/api/music/library/scan"),
    ("DELETE", "/api/music/library/folders"),
    ("POST", "/api/music/releases/12/tracks"),
    ("POST", "/api/music/artists/12/discography/sync"),
    ("PATCH", "/api/video/movies/12"),
    ("DELETE", "/api/video/movies/12"),
    ("POST", "/api/video/movies/12/grab"),
    ("GET", "/api/video/library/scan"),
    ("POST", "/api/music/releases/latest/download"),
    ("PATCH", "/api/video/series/12/seasons/../../../settings"),
]


def _route(method: str, path: str) -> bool:
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in app.routes)


@pytest.mark.parametrize(("method", "path"), EVERY_CALL)
def test_every_call_a_consumer_makes_is_a_route(method, path):
    assert _route(method, path)


@pytest.mark.parametrize(("consumer", "method", "path"),
                         [(name, *call) for name, calls in CALLS.items() for call in calls])
def test_each_token_may_make_every_call_its_consumer_makes(consumer, method, path):
    assert auth.allowed(RUNTIME, ROSTER, path, None, TOKENS[consumer], method)


@pytest.mark.parametrize(("consumer", "method", "path"),
                         [(name, *call) for name in ("downloads", "cameras")
                          for call in PLAYER if call not in CALLS[name]])
def test_a_narrow_token_opens_nothing_its_consumer_does_not_ask(consumer, method, path):
    assert not auth.allowed(RUNTIME, ROSTER, path, None, TOKENS[consumer], method)


@pytest.mark.parametrize(("consumer", "method", "path"),
                         [(name, *call) for name in TOKENS for call in REFUSED])
def test_no_token_keeps_the_install(consumer, method, path):
    assert not auth.allowed(RUNTIME, ROSTER, path, None, TOKENS[consumer], method)


def test_the_door_tells_a_module_it_is_refused_not_unknown(quick):
    async def scenario():
        await signed_in("boss")
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(key=auth.token_key("player"),
                                                         value=TOKENS["player"]))
            await session.commit()
        settings_store.forget_runtime()
        async with library() as client:
            client.headers[opus_auth.TOKEN_HEADER] = TOKENS["player"]
            people = await client.get("/api/auth/people")
            settings = await client.put("/api/settings", json={"opus_url": "http://elsewhere"})
            token = await client.get("/api/auth/token")
            del client.headers[opus_auth.TOKEN_HEADER]
            stranger = await client.get("/api/auth/token")
        async with db.SessionLocal() as session:
            stored = await session.get(Setting, "opus_url")
        return people, settings, token, stranger, stored

    people, settings, token, stranger, stored = run(scenario())
    assert people.status_code == 200
    assert (settings.status_code, token.status_code) == (403, 403)
    assert stranger.status_code == 401
    assert stored is None


def test_a_new_token_ends_only_its_own_consumers(quick):
    async def scenario():
        async with db.SessionLocal() as session:
            for name, token in TOKENS.items():
                await session.execute(insert(Setting).values(key=auth.token_key(name), value=token))
            await session.commit()
        settings_store.forget_runtime()
        async with library(await signed_in("boss")) as client:
            issued = await client.post("/api/auth/token", json={"consumer": "downloads"})
            unknown = await client.post("/api/auth/token", json={"consumer": "nobody"})
            listed = await client.get("/api/auth/token")
        async with library() as client:
            answers = {}
            for name, token in TOKENS.items():
                client.headers[opus_auth.TOKEN_HEADER] = token
                answers[name] = (await client.get("/api/auth/people")).status_code
            client.headers[opus_auth.TOKEN_HEADER] = issued.json()["token"]
            answers["issued"] = (await client.get("/api/auth/people")).status_code
        return issued, unknown, listed, answers

    issued, unknown, listed, answers = run(scenario())
    assert issued.status_code == 200 and issued.json()["consumer"] == "downloads"
    assert unknown.status_code == 404
    held = {row["consumer"]: row["token"] for row in listed.json()["tokens"]}
    assert held == {**TOKENS, "downloads": issued.json()["token"]}
    assert answers == {"player": 200, "downloads": 401, "cameras": 200, "issued": 200}
