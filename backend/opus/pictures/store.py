"""Where the library keeps the pictures it draws.

A catalogue hands out the address of a picture, not the picture, and an address
on somebody else's CDN is a promise they keep only when they are not busy:
Deezer's answers a busy minute with a 429, a 503, or a redirect onto its own
blank square, which a cache then keeps for a year. So the library takes each
picture once, keeps the original, and makes the sizes a screen asks for itself.

Where a record or a film already carries its picture on the disk — the tagger's
cover.jpg, the poster beside a film — that file is the original and nothing is
fetched at all.

  originals/  what came from a catalogue, at most MASTER_EDGE on its long side
  sized/      the widths screens ask for, made on first request and kept. A
              cache, and it says so to restic and to the Proxmox client.
  tiles/      the map's tiles, as OpenStreetMap asks them to be kept
"""

import asyncio
import datetime
import hashlib
import ipaddress
import logging
import os
import weakref
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update

from opus import http
from opus.db import SessionLocal
from opus.models import Picture
from opus.models.pictures import FAILED, HELD, NONE, WANTED
from opus.passes import spawn

log = logging.getLogger("opus.pictures")

ROOT = Path("/artwork")

# the trees a picture found beside a record or a film can be in
LIBRARY_TREES = ("/music/", "/movies/", "/television/", "/video/")

# A catalogue's thumbnail is what it listed, not what it has: the original is
# taken at the size the largest screen draws, and every smaller one made here.
MASTER_EDGE = 2048
# A cover at any size worth drawing is under a megabyte; past this it is not
# artwork, whatever it calls itself.
MOST_BYTES = 12 * 1024 * 1024
MOST_PIXELS = 50_000_000
# The widths kept. A width anybody could type would be a file per width
# anybody typed; a screen gets the smallest of these that covers what it draws.
WIDTHS = (160, 320, 480, 640, 960, 1280, 1920)
HOPS = 3

# md5("") — the picture id Deezer redirects to when it has no picture, and,
# when its CDN is busy, when it has one
DEEZER_EMPTY = "d41d8cd98f00b204e9800998ecf8427e"
# the blank square is what a busy CDN answers as often as a missing picture
# does, so it is believed only after this many tries spread over hours
BLANK_TRIES = 5

DRAWN_AS_IS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
# a picture that may be see-through stays see-through at every size
CLEAR = {"image/png", "image/webp", "image/gif", "image/svg+xml"}

FIRST_RETRY = datetime.timedelta(minutes=2)
LAST_RETRY = datetime.timedelta(days=1)

CACHEDIR_TAG = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# OPUS · Library picture sizes. Made again from the originals on request.\n"
)

# libvips threads inside one image; many small covers parallelise better
# one per worker, and both at once only fight over the same cores
os.environ.setdefault("VIPS_CONCURRENCY", "1")
_deriving = asyncio.Semaphore(4)


# what is known in this process to have no picture, so a list can say so
# instead of handing out an address that answers 404
nothing: set[int] = set()


class Unreachable(Exception):
    """The source did not answer this time."""

    def __init__(self, why: str, wait: float | None = None):
        super().__init__(why)
        self.wait = wait


class Nothing(Exception):
    """The source answered, and there is no picture there."""


class Blank(Exception):
    """The source answered with its blank square."""


def prepare() -> None:
    for sub in ("originals", "sized", "tiles", ".incoming"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)
    for cache in ("sized", "tiles"):
        (ROOT / cache / "CACHEDIR.TAG").write_text(CACHEDIR_TAG)
        (ROOT / cache / ".pxarexclude").write_text("*\n")


def in_library(source: str) -> bool:
    return source.startswith(LIBRARY_TREES)


def drawable(value: object) -> bool:
    return isinstance(value, str) and (value.startswith(("https://", "http://")) or in_library(value))


def _shard(picture_id: int) -> str:
    return f"{picture_id % 256:02x}"


def original_path(file: str) -> Path:
    """A file in the library tree is where it is; one in the store is named
    relative to it, so the store can move."""
    return Path(file) if file.startswith("/") else ROOT / file


def width_for(asked: int) -> int:
    return next((w for w in WIDTHS if w >= asked), WIDTHS[-1])


# ---------------------------------------------------------------------- fetch

_client: httpx.AsyncClient | None = None
_throttles: dict[str, http.Throttle] = {}
# Deezer's CDN throttles by address and the house shares one with the box this
# runs on, so it is asked slowest
PACE = {"cdn-images.dzcdn.net": 1.0, "e-cdns-images.dzcdn.net": 1.0}
DEFAULT_PACE = 0.2

# TMDB keeps a menu of sizes per kind of picture, and the catalogue asks each
# kind at its own: the next one up that a television still draws sharp. A
# backdrop and a still are drawn across the whole screen, so theirs is the
# original.
TMDB_MASTER = {"w342": "w780", "w780": "original", "w300": "original",
               "w185": "h632", "w92": "w500"}
TIDAL_EDGES = (1280, 750)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=30, follow_redirects=False,
            limits=httpx.Limits(max_connections=16),
            # Wikimedia answers an anonymous client library 403 by policy
            headers={"User-Agent": http.USER_AGENT})
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def throttle(host: str) -> http.Throttle:
    if host not in _throttles:
        _throttles[host] = http.Throttle(PACE.get(host, DEFAULT_PACE))
    return _throttles[host]


def masters(source: str) -> list[str]:
    """The same picture at the sizes the original is kept at, largest first.

    The catalogue keeps whatever size its first screen wanted: Deezer's 250
    for a release row, Tidal's 320, TMDB's w342 for a poster. Every source that
    names the size in its address is asked for its large one instead. Discogs
    is left alone because its addresses are signed over the size, and
    Wikipedia's own uploads because an original has no size to change."""
    parts = urlsplit(source)
    host = parts.hostname or ""
    path = parts.path
    head, sep, tail = path.rpartition("/")
    if host.endswith("dzcdn.net"):
        size, dash, rest = tail.partition("-")
        if dash and "x" in size:
            return [parts._replace(path=f"{head}{sep}1000x1000-{rest}").geturl()]
    if host == "image.tmdb.org" and path.startswith("/t/p/"):
        size, _, rest = path[5:].partition("/")
        return [parts._replace(path=f"/t/p/{TMDB_MASTER.get(size, 'original')}/{rest}").geturl()]
    if host.endswith("mzstatic.com") and "x" in tail.split("bb.")[0]:
        return [parts._replace(path=f"{head}{sep}{MASTER_EDGE}x{MASTER_EDGE}bb.{tail.rsplit('.', 1)[-1]}").geturl()]
    if host == "resources.tidal.com" and "x" in tail.split(".")[0]:
        # a cover goes to 1280; a face stops at 750 and refuses anything larger
        return [parts._replace(path=f"{head}{sep}{edge}x{edge}.{tail.rsplit('.', 1)[-1]}").geturl()
                for edge in TIDAL_EDGES]
    return []


def _blank(address: str) -> bool:
    return f"/{DEEZER_EMPTY}/" in address


def _inside(host: str) -> bool:
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return host in ("localhost", "")


async def _get(address: str) -> tuple[bytes, str]:
    """One picture's bytes and its type, following redirects by hand: a blank
    square at the end of one is not a picture, whatever the status says."""
    client = _http()
    for _ in range(HOPS + 1):
        if _blank(address):
            raise Blank(address)
        host = urlsplit(address).hostname or ""
        if _inside(host):
            raise Nothing(f"{host} is not a catalogue's picture host")
        pace = throttle(host)
        await pace.wait()
        try:
            async with client.stream("GET", address) as answer:
                if answer.is_redirect:
                    address = str(answer.url.join(answer.headers.get("location", "")))
                    continue
                if answer.status_code == 429 or answer.status_code >= 500:
                    wait = http.retry_after(answer)
                    if answer.status_code == 429:
                        pace.defer(wait or 60)
                    raise Unreachable(f"{host} answered {answer.status_code}", wait)
                if answer.status_code >= 400:
                    raise Nothing(f"{host} answered {answer.status_code}")
                kind = answer.headers.get("content-type", "").partition(";")[0].strip().lower()
                kind = "image/jpeg" if kind == "image/jpg" else kind
                if not kind.startswith("image/"):
                    raise Nothing(f"{host} answered {kind or 'nothing'}, not a picture")
                body = bytearray()
                async for chunk in answer.aiter_bytes():
                    body += chunk
                    if len(body) > MOST_BYTES:
                        raise Nothing(f"{host} sent more than {MOST_BYTES} bytes")
                return bytes(body), kind
        except httpx.HTTPError as exc:
            raise Unreachable(f"{host}: {type(exc).__name__}") from None
    raise Nothing(f"redirected more than {HOPS} times")


async def fetch(source: str) -> tuple[bytes, str]:
    """The original of a picture a catalogue named. A larger size that the
    source refuses is not a missing picture: the address as the catalogue gave
    it is asked before anything is concluded. A busy one is: the small size it
    might still answer would be kept for good."""
    for master in masters(source):
        if master == source:
            break
        try:
            return await _get(master)
        except Nothing:
            continue
    return await _get(source)


# --------------------------------------------------------------------- encode

def _encoding(clear: bool) -> tuple[str, str]:
    if clear:
        return ".webp", "[Q=88,alpha_q=100,keep=icc]"
    return ".jpg", "[Q=86,interlace,optimize_coding,keep=icc]"


def _kept(body: bytes, kind: str) -> tuple[bytes, str, int, int]:
    """The original as it is when it is already small enough and in a format
    every screen draws; otherwise the same picture at MASTER_EDGE."""
    import pyvips

    vector = kind == "image/svg+xml"
    try:
        header = pyvips.Image.new_from_buffer(body, "")
    except pyvips.Error as exc:
        raise Nothing(f"not a picture libvips can read: {str(exc).splitlines()[0]}") from None
    if header.width * header.height > MOST_PIXELS and not vector:
        raise Nothing(f"{header.width}x{header.height} is more picture than artwork holds")
    if (not vector and kind in DRAWN_AS_IS
            and max(header.width, header.height) <= MASTER_EDGE):
        return body, kind, header.width, header.height
    # a logo drawn as lines has no size of its own worth keeping, so it is
    # drawn at a size a screen will not outgrow
    image = pyvips.Image.thumbnail_buffer(
        body, MASTER_EDGE, height=MASTER_EDGE,
        size=pyvips.enums.Size.BOTH if vector else pyvips.enums.Size.DOWN)
    clear = image.hasalpha()
    suffix, options = _encoding(clear)
    made = image.write_to_buffer(suffix + options)
    return made, "image/webp" if clear else "image/jpeg", image.width, image.height


def _suffix(kind: str) -> str:
    return DRAWN_AS_IS.get(kind, ".jpg")


def _write_original(picture_id: int, body: bytes, kind: str) -> str:
    relative = Path("originals") / _shard(picture_id) / f"{picture_id}{_suffix(kind)}"
    target = ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = ROOT / ".incoming" / f"{picture_id}.{os.getpid()}{target.suffix}"
    staging.write_bytes(body)
    os.replace(staging, target)
    return str(relative)


def _stamp(original: Path) -> str:
    seen = original.stat()
    return hashlib.sha1(f"{original}|{seen.st_mtime_ns}|{seen.st_size}".encode()).hexdigest()[:12]


def _sized_path(picture_id: int, width: int, stamp: str, clear: bool) -> Path:
    suffix, _ = _encoding(clear)
    return ROOT / "sized" / str(width) / _shard(picture_id) / f"{picture_id}-{stamp}{suffix}"


def _derive(original: Path, target: Path, width: int, clear: bool) -> None:
    import pyvips

    # constrained by width alone: a poster asked for at 320 is 320 across
    image = pyvips.Image.thumbnail(str(original), width, height=10_000_000,
                                   size=pyvips.enums.Size.DOWN)
    target.parent.mkdir(parents=True, exist_ok=True)
    _, options = _encoding(clear)
    # the extension stays last: libvips picks the encoder from it
    staging = ROOT / ".incoming" / f"{target.stem}.{os.getpid()}{target.suffix}"
    image.write_to_file(str(staging) + options)
    if staging.stat().st_size == 0:
        raise RuntimeError(f"the encoder wrote nothing for {original}")
    os.replace(staging, target)


# ---------------------------------------------------------------------- rows

_holding: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def _later(tries: int, wait: float | None) -> datetime.datetime:
    backoff = min(FIRST_RETRY * (2 ** max(tries - 1, 0)), LAST_RETRY)
    if wait:
        backoff = max(backoff, datetime.timedelta(seconds=wait))
    return datetime.datetime.now(datetime.UTC) + backoff


def due(picture: Picture) -> bool:
    if picture.state == WANTED:
        return True
    if picture.state == FAILED:
        return picture.retry_at is None or picture.retry_at <= datetime.datetime.now(datetime.UTC)
    if picture.state == HELD:
        return picture.file is None or not original_path(picture.file).exists()
    return False


async def hold(picture_id: int) -> Picture | None:
    """Take the picture's original if it is not held, and say what it is now.

    One at a time per picture: a shelf of covers drawn on two screens at once
    asks for the same ones, and the CDN should be asked once."""
    lock = _holding.get(picture_id)
    if lock is None:
        lock = _holding[picture_id] = asyncio.Lock()
    async with lock:
        async with SessionLocal() as session:
            picture = await session.get(Picture, picture_id)
            if picture is None or not due(picture):
                return picture
            source, tries, before = picture.source, picture.tries, picture.file
        values = await _taken(picture_id, source, tries)
        async with SessionLocal() as session:
            await session.execute(update(Picture).where(Picture.id == picture_id).values(**values))
            await session.commit()
            picture = await session.get(Picture, picture_id)
        if picture.state == NONE:
            nothing.add(picture_id)
        if before and before != picture.file and not before.startswith("/"):
            (ROOT / before).unlink(missing_ok=True)
        return picture


async def _taken(picture_id: int, source: str, tries: int) -> dict:
    if in_library(source):
        # a file in the library that has gone has nothing to be fetched from
        if Path(source).is_file():
            return {"state": HELD, "file": source, "tries": 0, "retry_at": None}
        return {"state": NONE, "file": None}
    try:
        body, kind = await fetch(source)
        body, kind, width, height = await asyncio.to_thread(_kept, body, kind)
        file = await asyncio.to_thread(_write_original, picture_id, body, kind)
    except Nothing as why:
        log.info("picture %s: %s has none (%s)", picture_id, source, why)
        return {"state": NONE, "file": None}
    except Blank:
        if tries + 1 >= BLANK_TRIES:
            log.info("picture %s: %s is blank after %d tries", picture_id, source, tries + 1)
            return {"state": NONE, "file": None}
        return {"state": FAILED, "tries": tries + 1, "retry_at": _later(tries + 5, None)}
    except Unreachable as why:
        log.info("picture %s: %s, asked again later", picture_id, why)
        return {"state": FAILED, "tries": tries + 1, "retry_at": _later(tries + 1, why.wait)}
    return {"state": HELD, "file": file, "kind": kind, "width": width, "height": height,
            "tries": 0, "retry_at": None}


async def sized(picture: Picture, asked: int) -> tuple[Path, str]:
    """The file to send for a screen that draws the picture `asked` across."""
    original = original_path(picture.file)
    width = width_for(asked)
    kind = picture.kind or ("image/png" if original.suffix.lower() == ".png" else "image/jpeg")
    if picture.width and picture.width <= width and kind in DRAWN_AS_IS:
        return original, kind
    clear = kind in CLEAR or original.suffix.lower() in (".png", ".webp", ".gif")
    stamp = await asyncio.to_thread(_stamp, original)
    target = _sized_path(picture.id, width, stamp, clear)
    if not target.exists():
        async with _deriving:
            if not target.exists():
                await asyncio.to_thread(_derive, original, target, width, clear)
    return target, "image/webp" if clear else "image/jpeg"


async def original(source: str) -> tuple[bytes, str] | None:
    """The original's bytes, for whoever writes the picture into a file —
    taken through the store, so a cover tagged into a record is the one the
    shelf draws and the CDN is not asked a second time."""
    from opus.pictures import door

    picture_id = (await door.ids({source})).get(source)
    if picture_id is None:
        return None
    picture = await hold(picture_id)
    if picture is None or picture.state != HELD:
        return None
    path = original_path(picture.file)
    body = await asyncio.to_thread(path.read_bytes)
    return body, picture.kind or ("image/png" if path.suffix.lower() == ".png" else "image/jpeg")


async def gather(ids: list[int]) -> None:
    """Take what a list just named, one host at a time, so the screen that
    asked finds most of it held by the time it draws."""
    for picture_id in ids:
        try:
            await hold(picture_id)
        except Exception:
            log.exception("picture %s could not be taken", picture_id)


def gather_soon(ids: list[int]) -> None:
    if ids:
        spawn(gather(ids))


async def wanted(after: int, limit: int) -> list[tuple[int, str]]:
    now = datetime.datetime.now(datetime.UTC)
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Picture.id, Picture.source)
            .where(Picture.id > after,
                   (Picture.state == WANTED)
                   | ((Picture.state == FAILED) & ((Picture.retry_at.is_(None)) | (Picture.retry_at <= now))))
            .order_by(Picture.id).limit(limit))).all()
    return [(r.id, r.source) for r in rows]
