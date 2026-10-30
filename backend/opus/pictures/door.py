"""Every picture leaves the library as the library's own address.

Inside, a picture is where it was found: a Deezer cover, a TMDB poster, a file
beside a record. At the door each one becomes /api/pictures/{id}, whoever wrote
the route and whatever catalogue the list came from — a route added tomorrow
cannot hand a screen somebody else's CDN, because it is never asked to remember
not to. A picture the store already knows has nothing is handed out as nothing,
so the screen draws its letter instead of a broken image."""

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opus.db import SessionLocal
from opus.models import Picture
from opus.models.pictures import NONE, WANTED
from opus.pictures import store

ADDRESS = "/api/pictures/"

# The names a picture goes by in what the library answers. A value under one of
# them is a picture only when it has the shape of one, so a name shared with
# something else costs nothing.
KEYS = frozenset({
    "cover_url", "image_url", "poster_url", "backdrop_url", "still_url", "thumb_url",
    "profile_url", "poster", "logo", "picture", "thumb", "cover_image",
    "cover_medium", "cover_xl", "picture_medium", "picture_xl",
})

# source → id, for every source this process has handed out; an id never
# changes for a source, so it is never asked twice
_ids: dict[str, int] = {}

IN_THREAD = 256 * 1024


async def ids(sources: set[str]) -> dict[str, int]:
    """The id of each source, registering the ones the library has not seen;
    what was registered here is taken in the background straight away."""
    unknown = [s for s in sources if s not in _ids]
    if unknown:
        async with SessionLocal() as session:
            await session.execute(
                insert(Picture).values([{"source": s} for s in unknown])
                .on_conflict_do_nothing(index_elements=[Picture.source]))
            rows = (await session.execute(
                select(Picture.id, Picture.source, Picture.state)
                .where(Picture.source.in_(unknown)))).all()
            await session.commit()
        fresh = []
        for row in rows:
            _ids[row.source] = row.id
            if row.state == NONE:
                store.nothing.add(row.id)
            elif row.state == WANTED:
                fresh.append(row.id)
        store.gather_soon(fresh)
    return {s: _ids[s] for s in sources if s in _ids}


def address(picture_id: int | None) -> str | None:
    if picture_id is None or picture_id in store.nothing:
        return None
    return f"{ADDRESS}{picture_id}"


def _found(node, into: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in KEYS and store.drawable(value):
                into.add(value)
            elif isinstance(value, (dict, list)):
                _found(value, into)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                _found(value, into)


def _swapped(node, by: dict[str, int]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in KEYS and store.drawable(value):
                node[key] = address(by.get(value))
            elif isinstance(value, (dict, list)):
                _swapped(value, by)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                _swapped(value, by)


async def ours(payload):
    """The same answer, with every picture in it at the library's address."""
    found: set[str] = set()
    _found(payload, found)
    if found:
        _swapped(payload, await ids(found))
    return payload


def _json(status: int, headers: Headers) -> bool:
    kind = headers.get("content-type", "").split(";")[0].strip().lower()
    return kind == "application/json" and status not in (204, 304) \
        and "content-encoding" not in headers


def _decode(body: bytes):
    return json.loads(body)


def _encode(payload) -> bytes:
    # what Starlette's JSONResponse writes, so nothing else about the answer moves
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=None,
                      separators=(",", ":")).encode("utf-8")


def _maybe(body: bytes) -> bool:
    return b'"http' in body or any(f'"{tree}'.encode() in body for tree in store.LIBRARY_TREES)


class Pictures:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        start: Message | None = None
        chunks: list[bytes] = []

        async def answer(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                if _json(message["status"], Headers(raw=message["headers"])):
                    start = message
                else:
                    await send(message)
                return
            if start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            if _maybe(body):
                big = len(body) > IN_THREAD
                payload = await asyncio.to_thread(_decode, body) if big else _decode(body)
                payload = await ours(payload)
                body = await asyncio.to_thread(_encode, payload) if big else _encode(payload)
            headers = MutableHeaders(raw=list(start["headers"]))
            headers["content-length"] = str(len(body))
            await send({**start, "headers": headers.raw})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, answer)

