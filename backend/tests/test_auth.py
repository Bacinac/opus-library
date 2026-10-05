import asyncio
import json
from compression import zstd

import opus_auth
import pytest

from conftest import library, run, signed_in
from opus import accounts, auth, db
from opus.config import settings
from opus.settings_store import RuntimeConfig

TOKENS = {"player": "player-token-0123456789", "downloads": "downloads-token-0123456789",
          "cameras": "cameras-token-0123456789"}
RUNTIME = RuntimeConfig({auth.token_key(name): token for name, token in TOKENS.items()})
ROSTER = {
    "boss": (2, "admin", False),
    "filip": (3, "user", False),
    "gost": (1, "guest", False),
    "kata": (5, "user", True),
}


def cookie(name: str, version: int) -> str:
    return opus_auth.issue(settings.session_key, name, version)


CALLERS = {
    "admin": {"cookie": cookie("boss", 2)},
    "user": {"cookie": cookie("filip", 3)},
    "guest": {"cookie": cookie("gost", 1)},
    "disabled": {"cookie": cookie("kata", 5)},
    "stale": {"cookie": cookie("filip", 2)},
    "stranger": {"cookie": cookie("nobody", 1)},
    "forged": {"cookie": opus_auth.issue("another-key-0123456789abcdef0123", "boss", 2)},
    "player": {"token": TOKENS["player"]},
    "downloads": {"token": TOKENS["downloads"]},
    "cameras": {"token": TOKENS["cameras"]},
    "wrong-token": {"token": "not-the-token"},
    "anonymous": {},
}

# path, method, and who may: everyone not named is refused
MATRIX = [
    ("/api/ping", "GET", "everyone"),
    ("/api/ready", "GET", "everyone"),
    ("/api/auth/login", "POST", "everyone"),
    ("/api/auth/logout", "POST", "everyone"),
    ("/api/auth/password", "POST", {"admin", "user", "guest"}),
    ("/api/settings", "GET", {"admin"}),
    ("/api/settings", "PUT", {"admin"}),
    ("/api/auth/token", "GET", {"admin"}),
    ("/api/auth/people", "GET", {"admin", "player", "downloads", "cameras"}),
    ("/api/auth/verify", "POST", {"admin", "player", "downloads"}),
    ("/api/auth/passkey/options", "POST", "everyone"),
    ("/api/auth/passkey/login", "POST", "everyone"),
    ("/api/auth/passkey/verify", "POST", {"admin", "player", "downloads"}),
    ("/api/auth/passkeys", "GET", {"admin", "user", "guest"}),
    ("/api/auth/passkeys/options", "POST", {"admin", "user", "guest"}),
    ("/api/auth/passkeys/3", "DELETE", {"admin", "user", "guest"}),
    ("/api/auth/devices/ask", "POST", {"admin", "player"}),
    ("/api/auth/devices", "GET", {"admin"}),
    ("/api/auth/devices/7", "PATCH", {"admin"}),
    ("/api/downloads", "GET", {"admin"}),
    ("/api/storage", "GET", {"admin", "player"}),
    ("/api/landing", "GET", {"admin"}),
    ("/api/music/library/scan", "GET", {"admin"}),
    ("/api/music/library/stats", "GET", {"admin", "player"}),
    ("/api/video/library/scan", "GET", {"admin"}),
    ("/api/photos/library/keeping-up", "GET", {"admin"}),
    ("/api/photos/library/underivable", "GET", {"admin"}),
    ("/api/photos/contacts", "GET", {"admin"}),
    ("/api/photos/contacts/sync", "POST", {"admin"}),
    ("/api/music/artists", "GET", {"admin", "user", "guest", "player"}),
    ("/api/music/artists", "POST", {"admin", "player"}),
    ("/api/music/releases/7/tracks", "GET", {"admin", "user", "guest", "player"}),
    ("/api/music/releases/7/tracks", "POST", {"admin"}),
    ("/api/video/movies", "GET", {"admin", "user", "guest", "player"}),
    ("/api/video/movies/3", "DELETE", {"admin"}),
    ("/api/musical", "GET", {"admin", "user", "player"}),
    ("/api/photos/timeline", "GET", {"admin", "user", "player"}),
    ("/api/photos/people", "GET", {"admin", "user", "player", "cameras"}),
    ("/api/photos/people/12/faces", "GET", {"admin", "user", "player", "cameras"}),
    ("/api/photos/people/12/faces", "POST", {"admin"}),
    ("/api/photos/" + "ab" * 20 + "/preview", "GET", {"admin", "user", "player", "cameras"}),
    ("/api/photos/" + "ab" * 20 + "/original", "GET", {"admin", "user", "player"}),
    ("/api/photos/faces/9/crop", "GET", {"admin", "user", "player", "cameras"}),
    ("/api/photos/clusters/4/person", "PUT", {"admin"}),
    ("/api/photos/vault", "GET", {"admin", "user"}),
    ("/api/photos/vault", "POST", {"admin", "user"}),
    ("/api/photos/vault/ab12/thumb", "PUT", {"admin", "user"}),
    ("/api/photos/offer", "POST", {"admin", "user"}),
    ("/api/photos/vaultX", "POST", {"admin"}),
    ("/api/photos/offering", "POST", {"admin"}),
]


@pytest.mark.parametrize(("path", "method", "may"), MATRIX)
@pytest.mark.parametrize("caller", list(CALLERS))
def test_who_may_do_what(path, method, may, caller):
    said = CALLERS[caller]
    allowed = auth.allowed(RUNTIME, ROSTER, path, said.get("cookie"), said.get("token"), method)
    assert allowed == (may == "everyone" or caller in may)


def test_an_install_without_an_admin_exposes_only_the_bootstrap_door():
    for path in auth.OPEN_PATHS:
        assert auth.allowed(RUNTIME, {}, path, None, None, "POST")
    assert not auth.allowed(RUNTIME, {}, "/api/settings", None, None, "PUT")


def test_ready_confirms_the_database_is_available():
    async def scenario():
        async with library() as client:
            return await client.get("/api/ready")

    answer = run(scenario())
    assert answer.status_code == 200 and answer.json() == {"ok": True}


def test_bootstrap_key_creates_one_admin(quick, monkeypatch):
    key = "bootstrap-key-which-is-long-enough-0123456789"
    monkeypatch.setattr(settings, "bootstrap_key", key)

    async def scenario():
        body = {"name": "first", "password": "a-long-enough-secret"}
        async with library() as client:
            wrong = await client.post(auth.BOOTSTRAP_PATH, json=body)
            made = await client.post(auth.BOOTSTRAP_PATH, json=body,
                                     headers={auth.BOOTSTRAP_HEADER: key})
            again = await client.post(auth.BOOTSTRAP_PATH, json=body,
                                      headers={auth.BOOTSTRAP_HEADER: key})
        return wrong, made, again

    wrong, made, again = run(scenario())
    assert wrong.status_code == 401
    assert (made.status_code, made.json()["role"]) == (200, "admin")
    assert again.status_code == 409


def test_an_unset_token_opens_nothing():
    runtime = RuntimeConfig({auth.token_key(name): "" for name in auth.CONSUMERS})
    assert not auth.allowed(runtime, ROSTER, "/api/music/artists", None, "", "GET")
    assert auth.consumer(runtime, "") is None


def test_standing_follows_the_roster():
    assert auth.standing(ROSTER, cookie("boss", 2)) == "admin"
    assert auth.standing(ROSTER, cookie("kata", 5)) is None
    assert auth.standing(ROSTER, cookie("filip", 2)) is None
    assert auth.whoami(ROSTER, cookie("gost", 1)) == "gost"


@pytest.mark.parametrize("change", ["remove", "role", "disabled", "secret"])
def test_an_inflight_roster_read_cannot_restore_revoked_access(quick, change):
    async def scenario():
        async with db.SessionLocal() as session:
            person = await accounts.add(session, "boss", "a-long-enough-secret", role="admin")
            original = cookie(person.name, person.version)
            identity = person.id
        began, released = asyncio.Event(), asyncio.Event()
        async with db.SessionLocal() as reader:
            class Delayed:
                first = True

                async def execute(self, query):
                    rows = await reader.execute(query)
                    if self.first:
                        self.first = False
                        began.set()
                        await released.wait()
                    return rows

            reading = asyncio.create_task(auth.roster(Delayed()))
            await asyncio.wait_for(began.wait(), 5)
            try:
                async with db.SessionLocal() as writer:
                    person = await writer.get(auth.User, identity)
                    if change == "remove":
                        await accounts.remove(writer, person)
                    else:
                        values = {"role": "user", "disabled": True, "secret": "a-new-long-enough-secret"}
                        await accounts.amend(writer, person, **{change: values[change]})
            finally:
                released.set()
            current = await asyncio.wait_for(reading, 5)
        assert current == auth._held
        assert not auth.allowed(RuntimeConfig({}), current, "/api/settings", original)
        if change == "role":
            assert auth.standing(current, original) == "user"
        else:
            assert auth.whoami(current, original) is None

    run(scenario())


@pytest.mark.parametrize("first", ["role", "disabled", "remove"])
@pytest.mark.parametrize("second", ["role", "disabled", "remove"])
def test_concurrent_admin_changes_leave_an_active_administrator(quick, monkeypatch, first, second):
    original_lock, original_count = accounts.lock_roster, accounts.admins

    async def scenario():
        cookies = {name: await signed_in(name) for name in ("boss", "other")}
        counted, release, entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        locks = 0

        async def lock(session):
            nonlocal locks
            locks += 1
            if locks == 2:
                entered.set()
            await original_lock(session)

        async def count(session):
            answer = await original_count(session)
            if not counted.is_set():
                counted.set()
                await release.wait()
            return answer

        monkeypatch.setattr(accounts, "lock_roster", lock)
        monkeypatch.setattr(accounts, "admins", count)

        async def change(name, operation):
            async with library(cookies[name]) as client:
                target = ("other" if name == "boss" else "boss") if operation == "remove" else name
                path = f"/api/auth/people/{target}"
                if operation == "remove":
                    return await client.delete(path)
                value = "user" if operation == "role" else True
                return await client.patch(path, json={operation: value})

        one = asyncio.create_task(change("boss", first))
        await asyncio.wait_for(counted.wait(), 5)
        two = asyncio.create_task(change("other", second))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            release.set()
        answers = await asyncio.wait_for(asyncio.gather(one, two), 5)
        assert answers[0].status_code == 200
        assert answers[1].status_code in (200, 403, 409)
        async with db.SessionLocal() as session:
            assert await original_count(session) >= 1

    run(scenario())


@pytest.mark.parametrize("path", ["/api/auth/logout", "/api/auth/login"])
def test_a_page_elsewhere_cannot_use_the_open_door(clean, path):
    async def scenario():
        cookie = await signed_in("boss")
        body = {"username": "boss", "password": "a-long-enough-secret"}
        answers = {}
        async with library() as client:
            for site in ("cross-site", "same-site"):
                answers[site] = await client.post(path, json=body,
                                                  headers={"sec-fetch-site": site})
            answers["elsewhere"] = await client.post(
                path, json=body, headers={"origin": "https://evil.example"})
            answers["here"] = await client.post(path, json=body,
                                                headers={"sec-fetch-site": "same-origin"})
            answers["module"] = await client.post(path, json=body)
        async with library(cookie) as client:
            answers["sibling with a session"] = await client.post(
                path, json=body, headers={"sec-fetch-site": "same-site"})
        return answers

    answers = run(scenario())
    for refused in ("cross-site", "same-site", "elsewhere", "sibling with a session"):
        assert answers[refused].status_code == 403, refused
        assert "set-cookie" not in answers[refused].headers, refused
    assert answers["here"].status_code == 200
    assert answers["module"].status_code == 200


def test_a_full_line_for_a_comparison_is_a_clear_refusal(quick, monkeypatch):
    async def scenario():
        await signed_in("boss")
        monkeypatch.setattr(accounts, "_queued", accounts.HASHING + accounts.WAITING)
        async with library() as client:
            return await client.post("/api/auth/login",
                                     json={"username": "boss", "password": "a-long-enough-secret"})

    answer = run(scenario())
    assert answer.status_code == 429
    assert answer.headers["retry-after"] == str(accounts.Busy.retry_after)
    assert answer.json()["detail"] == "too many sign-ins at once"


def test_a_password_change_relayed_by_a_module_counts_against_the_person(quick):
    async def scenario():
        filip, kata = await signed_in("filip", role="user"), await signed_in("kata", role="user")
        async with library(filip) as client:
            wrong = [await client.post("/api/auth/password",
                                       json={"current": "not-it", "password": "a-new-long-secret"})
                     for _ in range(accounts.FAILS_PER_NAME_FROM_ADDRESS)]
            locked = await client.post("/api/auth/password", json={
                "current": "a-long-enough-secret", "password": "a-new-long-secret"})
        async with library(kata) as client:
            other = await client.post("/api/auth/password", json={
                "current": "a-long-enough-secret", "password": "a-new-long-secret"})
        return wrong, locked, other

    wrong, locked, other = run(scenario())
    assert {w.status_code for w in wrong} == {401}
    assert locked.status_code == 429
    assert other.status_code == 200


def test_every_answer_carries_the_browser_headers(clean):
    async def scenario():
        async with library() as client:
            return [await client.get("/api/ping"), await client.get("/api/settings")]

    for answer in run(scenario()):
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert answer.headers["referrer-policy"] == "same-origin"
        assert answer.headers["content-security-policy"] == "frame-ancestors 'self'"


def test_a_json_answer_leaves_the_door_compressed(clean):
    async def scenario():
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return await client.get("/api/settings", headers={"accept-encoding": "zstd"})

    answer = run(scenario())
    assert answer.headers["content-encoding"] == "zstd"
    assert json.loads(zstd.decompress(answer.content))


def test_the_api_does_not_describe_itself(clean):
    async def scenario():
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return [(await client.get(path)).status_code for path in ("/docs", "/redoc", "/openapi.json")]

    assert run(scenario()) == [404, 404, 404]
