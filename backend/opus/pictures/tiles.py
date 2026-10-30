"""The map's tiles, kept the way OpenStreetMap asks a client to keep them.

Its tile servers are run on donations and their policy is plain: say who is
asking, keep what was given, and do not ask for the same tile again within a
week. A screen that drew the map straight from them asked every time it was
opened, from every screen in the house, and named nobody."""

import asyncio
import datetime
import os
import time
from pathlib import Path

import httpx

from opus import http
from opus.pictures import store

SOURCE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DEEPEST = 19
# a week is the policy's least; a house's streets do not move that fast
FRESH = datetime.timedelta(days=30).total_seconds()
# the policy's limit on connections at once
_asking = asyncio.Semaphore(2)
_client: httpx.AsyncClient | None = None


class NoTile(Exception):
    pass


def valid(z: int, x: int, y: int) -> bool:
    return 0 <= z <= DEEPEST and 0 <= x < 2 ** z and 0 <= y < 2 ** z


def _path(z: int, x: int, y: int) -> Path:
    return store.ROOT / "tiles" / str(z) / str(x) / f"{y}.png"


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20, headers={"User-Agent": http.USER_AGENT})
    return _client


def _write(target: Path, body: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = store.ROOT / ".incoming" / f"tile-{target.parent.parent.name}-{target.parent.name}-{target.name}.{os.getpid()}"
    staging.write_bytes(body)
    os.replace(staging, target)


async def tile(z: int, x: int, y: int) -> Path:
    """The tile, from the store when it is fresh; asked again when it is not,
    and the stale one kept when asking fails — an old street is still a street."""
    target = _path(z, x, y)
    try:
        age = time.time() - target.stat().st_mtime
    except FileNotFoundError:
        age = None
    if age is not None and age < FRESH:
        return target
    try:
        async with _asking:
            answer = await _http().get(SOURCE.format(z=z, x=x, y=y))
        answer.raise_for_status()
        if not answer.headers.get("content-type", "").startswith("image/"):
            raise NoTile(f"the tile server answered {answer.headers.get('content-type')}")
    except (httpx.HTTPError, NoTile) as why:
        if age is not None:
            return target
        raise NoTile(str(why)) from None
    await asyncio.to_thread(_write, target, answer.content)
    return target
