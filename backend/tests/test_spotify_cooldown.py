import httpx
import pytest

from conftest import run
from opus.music.metadata import spotify
from opus.music.metadata.spotify import SpotifyClient, SpotifyCooldown


def test_a_days_ban_outlives_the_process_that_was_told(clean, monkeypatch):
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(429, headers={"Retry-After": "86400"})

    def client() -> SpotifyClient:
        made = SpotifyClient("id", "secret")
        made._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        return made

    monkeypatch.setattr(spotify, "_cooldown_until", None)
    first = client()
    with pytest.raises(SpotifyCooldown, match="Retry-After 86400s"):
        run(first._get("/artists/x/albums"))
    run(first.close())
    assert calls == ["/api/token", "/v1/artists/x/albums"]

    monkeypatch.setattr(spotify, "_cooldown_until", None)
    after_restart = client()
    with pytest.raises(SpotifyCooldown, match="in rate-limit cooldown"):
        run(after_restart._get("/artists/x/albums"))
    run(after_restart.close())
    assert calls == ["/api/token", "/v1/artists/x/albums"]
