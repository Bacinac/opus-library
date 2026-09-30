import datetime
import json
import re

import httpx
import opus_auth
import pytest

from conftest import run
from opus import acquire
from opus.settings_store import RuntimeConfig

CONFIG = RuntimeConfig({"opus_url": "http://downloads.test/", "opus_token": "t0ken"})


class Downloads:
    def __init__(self):
        self.asked: list[httpx.Request] = []
        self.answers: dict[tuple[str, str], httpx.Response] = {}
        self.down = False
        self.fail_grab_once = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        self.asked.append(request)
        if self.fail_grab_once and (request.method, request.url.path) == ("POST", "/api/grab"):
            self.fail_grab_once = False
            raise httpx.ReadTimeout("response lost", request=request)
        return self.answers.get((request.method, request.url.path), httpx.Response(404))


@pytest.fixture
def downloads(monkeypatch):
    service = Downloads()
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(service))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    yield service
    run(acquire.close())


def _job(**fields) -> httpx.Response:
    return httpx.Response(200, json={"state": "downloading", **fields})


def test_a_job_is_read_as_the_library_needs_it(downloads):
    downloads.answers[("GET", "/api/jobs/7")] = _job(
        progress=0.5, error="", detail="slow", landing_path="/landing/music/x")

    async def scenario():
        try:
            return await acquire.job_status(CONFIG, {"job": "7"})
        finally:
            await acquire.close()

    status = run(scenario())
    assert (status.state, status.progress, status.detail, status.directory) == (
        "downloading", 0.5, "slow", "/landing/music/x")
    request = downloads.asked[0]
    assert str(request.url) == "http://downloads.test/api/jobs/7"
    assert request.headers[opus_auth.TOKEN_HEADER] == "t0ken"


def test_a_job_downloads_does_not_know_is_unknown_not_failed(downloads):
    async def scenario():
        try:
            return (await acquire.job_status(CONFIG, {"job": "8"}),
                    await acquire.job_status(CONFIG, {}))
        finally:
            await acquire.close()

    gone, never = run(scenario())
    assert (gone.state, never.state) == ("unknown", "unknown")
    assert len(downloads.asked) == 1


@pytest.mark.parametrize("answer", [
    httpx.Response(500, text="boom"),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"progress": 1}),
])
def test_whatever_goes_wrong_reading_a_job_is_an_acquire_error(downloads, answer):
    downloads.answers[("GET", "/api/jobs/9")] = answer

    async def scenario():
        try:
            with pytest.raises(acquire.AcquireError):
                await acquire.job_status(CONFIG, {"job": "9"})
        finally:
            await acquire.close()

    run(scenario())


def test_an_unreachable_downloads_is_an_acquire_error(downloads):
    downloads.down = True

    async def scenario():
        try:
            for call in (acquire.job_status(CONFIG, {"job": "1"}),
                         acquire.drop(CONFIG, {"job": "1"}),
                         acquire.grab(CONFIG, {"id": 1}, "music"),
                         acquire.search(CONFIG, "q", "music", ["slskd"]),
                         acquire.inspect(CONFIG, {"id": 1})):
                with pytest.raises(acquire.AcquireError):
                    await call
        finally:
            await acquire.close()

    run(scenario())


def test_dropping_accepts_gone_and_refuses_the_rest(downloads):
    downloads.answers[("DELETE", "/api/jobs/1")] = httpx.Response(204)
    downloads.answers[("DELETE", "/api/jobs/3")] = httpx.Response(409, text="still importing")

    async def scenario():
        try:
            await acquire.drop(CONFIG, {"job": "1"})
            await acquire.drop(CONFIG, {"job": "2"})
            await acquire.drop(CONFIG, {})
            with pytest.raises(acquire.AcquireError, match="still importing"):
                await acquire.drop(CONFIG, {"job": "3"})
        finally:
            await acquire.close()

    run(scenario())
    assert [r.url.path for r in downloads.asked] == ["/api/jobs/1", "/api/jobs/2", "/api/jobs/3"]


def test_a_grab_says_who_asked_and_under_which_namespace(downloads):
    downloads.answers[("POST", "/api/grab")] = httpx.Response(200, json={"job_id": "42"})

    async def scenario():
        try:
            return await acquire.grab(CONFIG, {"id": 5}, "movies")
        finally:
            await acquire.close()

    assert run(scenario()) == "42"
    assert json.loads(downloads.asked[0].content) == {
        "grab_ref": {"id": 5}, "namespace": "movies", "app": acquire.APP}
    assert re.fullmatch(r"[0-9a-f]{32}", downloads.asked[0].headers["Idempotency-Key"])


def test_search_reads_releases_and_refuses_an_engine_error(downloads):
    downloads.answers[("POST", "/api/search")] = httpx.Response(
        200, json={"releases": [{"title": "X"}], "errors": []})

    async def successful():
        try:
            return await acquire.search(CONFIG, "q", "music", ["slskd"])
        finally:
            await acquire.close()

    assert run(successful()) == [{"title": "X"}]
    downloads.answers[("POST", "/api/search")] = httpx.Response(
        200, json={"releases": [], "errors": [{"engine": "slskd", "detail": "unreachable"}]})

    async def failed():
        try:
            with pytest.raises(acquire.AcquireError, match="slskd: unreachable"):
                await acquire.search(CONFIG, "q", "music", ["slskd"])
        finally:
            await acquire.close()

    run(failed())


def test_a_provided_grab_key_is_sent(downloads):
    downloads.answers[("POST", "/api/grab")] = httpx.Response(200, json={"job_id": "42"})

    async def scenario():
        try:
            return await acquire.grab(CONFIG, {"id": 5}, "movies", idempotency_key="a" * 32)
        finally:
            await acquire.close()

    assert run(scenario()) == "42"
    assert downloads.asked[0].headers["Idempotency-Key"] == "a" * 32


def test_a_transport_retry_keeps_the_same_grab_key(downloads):
    downloads.fail_grab_once = True
    downloads.answers[("POST", "/api/grab")] = httpx.Response(200, json={"job_id": "42"})

    async def scenario():
        try:
            return await acquire.grab(CONFIG, {"id": 5}, "movies")
        finally:
            await acquire.close()

    assert run(scenario()) == "42"
    assert len(downloads.asked) == 2
    assert downloads.asked[0].headers["Idempotency-Key"] == downloads.asked[1].headers["Idempotency-Key"]


def test_no_address_is_said_before_anything_is_asked(downloads):
    async def scenario():
        with pytest.raises(acquire.AcquireError, match="no OPUS url"):
            await acquire.job_status(RuntimeConfig({"opus_url": ""}), {"job": "1"})

    run(scenario())
    assert downloads.asked == []


def test_a_changed_address_or_token_closes_the_old_client(downloads):
    downloads.answers[("GET", "/api/jobs/1")] = _job()
    rotated = RuntimeConfig({**CONFIG.values, "opus_token": "new-t0ken"})

    async def scenario():
        await acquire.job_status(CONFIG, {"job": "1"})
        old = next(iter(acquire._clients.values()))
        await acquire.job_status(rotated, {"job": "1"})
        held = list(acquire._clients)
        closed = old.is_closed
        await acquire.close()
        return closed, held, acquire._clients

    closed, held, after = run(scenario())
    assert closed
    assert held == [("http://downloads.test", "new-t0ken")]
    assert after == {}
    assert downloads.asked[-1].headers[opus_auth.TOKEN_HEADER] == "new-t0ken"


def test_only_a_job_unknown_past_its_grace_is_unaccounted_for():
    now = datetime.datetime.now(datetime.UTC)
    unknown = acquire.JobStatus("unknown")
    assert not acquire.unaccounted_for(unknown, now - acquire.UNACCOUNTED_GRACE / 2)
    assert acquire.unaccounted_for(unknown, now - acquire.UNACCOUNTED_GRACE * 2)
    assert not acquire.unaccounted_for(acquire.JobStatus("queued"),
                                       now - acquire.UNACCOUNTED_GRACE * 2)
