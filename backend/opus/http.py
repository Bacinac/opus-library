import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

USER_AGENT = "OPUS-Library/0.1 (https://boskovic.biz)"

log = logging.getLogger("opus.http")


class Throttle:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            while (delay := self._next - time.monotonic()) > 0:
                await asyncio.sleep(delay)
            self._next = time.monotonic() + self.interval

    def defer(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


def retry_after(resp: httpx.Response) -> float | None:
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def overloaded(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or resp.status_code >= 500


async def get(client: httpx.AsyncClient, url: str, *, attempts: int = 4,
              pause: float = 5.0, longest: float = 120.0,
              throttle: Throttle | None = None,
              busy: Callable[[httpx.Response], bool] = overloaded,
              **kwargs) -> httpx.Response:
    """GET that waits out a busy upstream. A busy answer that outlasts the
    attempts, or asks for a longer wait than `longest`, is returned for the
    caller to judge; a transport error on the last attempt is raised."""
    target = client.base_url.join(url)
    attempt = 0
    while True:
        attempt += 1
        if throttle is not None:
            await throttle.wait()
        try:
            resp = await client.get(url, **kwargs)
        except httpx.TransportError as exc:
            if attempt >= attempts:
                raise
            wait, why = pause * attempt, type(exc).__name__
        else:
            if not busy(resp):
                return resp
            wait = retry_after(resp)
            if wait is None:
                wait = pause * attempt
            if attempt >= attempts or wait > longest:
                return resp
            why = f"HTTP {resp.status_code}" if resp.status_code >= 400 else "rate limit"
        log.info("%s on %s%s, retrying in %.0fs (attempt %d of %d)",
                 why, target.host, target.path, wait, attempt, attempts)
        if throttle is not None:
            throttle.defer(wait)
        else:
            await asyncio.sleep(wait)
