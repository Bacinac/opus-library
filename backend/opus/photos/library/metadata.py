"""When a photograph was taken, and how much that answer is worth.

A capture time is not one fact but five, of falling confidence, and a library
that shows them in the same type invites the reader to trust them equally. So
the moment is stored beside the reason we believe it:

  exif      the camera wrote it down. With OffsetTimeOriginal it is an instant;
            without one it is a wall clock and the zone is an assumption.
  filename  somebody, or some export tool, put a date in the name. Often right,
            occasionally the day the file was copied.
  path      the directory it was filed in says a month. Weaker than a filename,
            which at least names a minute, but it is what remains for a
            photograph whose EXIF was stripped before it ever arrived here.
  mtime     the filesystem's opinion, which is really "when this copy appeared".
  none      nothing at all, and the timeline must be able to say so.

EXIF belongs to the content and travels with it; a filename belongs to one path,
and the same photograph often sits at several. Where they disagree the better
source wins, and among equals the earliest — a copy is made after the original,
never before it.
"""

import asyncio
import datetime
import json
import logging
import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from opus.db import SessionLocal
from opus.models import (
    FILE_PRESENT,
    TIME_EXIF,
    TIME_FILENAME,
    TIME_FILE_MTIME,
    TIME_NONE,
    TIME_PATH,
    TIME_PEOPLE,
    Photo,
)
from opus.passes import Pass
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

# One process per batch rather than per file: exiftool spends most of a short
# run starting Perl, and 45,000 of those is an hour of nothing.
BATCH = 200

TAGS = ("-DateTimeOriginal", "-CreateDate", "-OffsetTimeOriginal",
        "-SubSecTimeOriginal", "-Make", "-Model",
        "-ImageWidth", "-ImageHeight", "-MIMEType",
        # how the camera was held: the width and height above are the sensor's,
        # and a picture that is to be shown turned is the other shape
        "-Orientation", "-Rotation",
        # Apple writes the same identifier into both halves of a Live Photo,
        # which is the only honest way to pair them: names and timestamps agree
        # for burst frames too.
        "-ContentIdentifier", "-MediaGroupUUID",
        # Where and how. Asked for in the same run that already opens every file,
        # so it costs the parsing and not the reading. -n makes exiftool answer
        # in numbers rather than in "45 deg 4' 43.20\" N", which is a sentence
        # about a number and has to be taken apart again.
        "-GPSLatitude", "-GPSLongitude", "-GPSAltitude",
        "-LensModel", "-LensID", "-FocalLength", "-FNumber",
        "-ISO", "-ExposureTime")

# The tree a photograph was filed in: <user>/YYYY/MM/. Month precision only, so
# the day is left at the first — a timeline that placed it on the 15th would be
# inventing a day nobody recorded.
PATH_MONTH = re.compile(r"/(\d{4})/(\d{2})/[^/]+$")


def _number(value) -> float | None:
    """A number exiftool gave us, or nothing. It answers in numbers when asked
    with -n, but a tag it could not read comes back as a word."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def _is_epoch(y: int, mo: int) -> bool:
    """January 1970 is not a month, it is what a system writes when it has no
    date at all. Seven photographs here are filed under it, and every one is an
    iPhone picture — inheriting that would put them at the head of the timeline
    and call it a fact. Falling through to the filesystem is less precise and
    more honest: it at least says it is only describing the copy."""
    return y == 1970 and mo == 1

# What a date in a filename looks like once the archive is actually read:
# "20130706-154433-181.83 KB.jpg", "20251109_113514.jpg", "IMG_20130706_154433.jpg",
# "PHOTO-2024-11-10-00-12-27.jpg", "2013-07-06 15.44.33.jpg"
FILENAME_DATES = (
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[-_ ]?(\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})[-.](\d{2})[-.](\d{2})[-_ T]+(\d{2})[-.:](\d{2})[-.:](\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})[-.](\d{2})[-.](\d{2})(?!\d)"),
)

EXIF_DT = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")

job = Pass("metadata", total=0, processed=0, dated=0, from_exif=0, from_filename=0,
           from_path=0, from_mtime=0, undated=0, paired=0, current="")


def _on_its_side(row: dict) -> bool:
    """Whether the picture is shown a quarter-turn from how it was stored:
    EXIF orientations 5 to 8 for a photograph, a 90 or 270 rotation for a
    recording."""
    return row.get("Orientation") in (5, 6, 7, 8) or row.get("Rotation") in (90, 270)


def _exiftool(paths: list[str]) -> dict[str, dict]:
    """One exiftool run over a batch, keyed by path. -n keeps numbers numeric."""
    out = subprocess.run(
        ["exiftool", "-json", "-n", "-q", "-q", *TAGS, *paths],
        capture_output=True, text=True, timeout=300)
    if not out.stdout.strip():
        return {}
    try:
        return {row.get("SourceFile", ""): row for row in json.loads(out.stdout)}
    except json.JSONDecodeError:
        log.warning("exiftool returned something that is not JSON for %d files", len(paths))
        return {}


def _from_exif(row: dict, zone: ZoneInfo) -> tuple[datetime.datetime | None, str]:
    """The camera's own answer, and the offset it recorded if it recorded one.

    Without an offset this is a wall clock with no zone. Reading it in the
    installation's zone is right for the overwhelming majority of a family
    archive and wrong for the holiday photographs; recording that the offset was
    absent is what lets a reader know which of the two they are looking at."""
    raw = row.get("DateTimeOriginal") or row.get("CreateDate")
    if not isinstance(raw, str):
        return None, ""
    m = EXIF_DT.match(raw)
    if not m:
        return None, ""
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    if y < 1900:
        return None, ""
    offset = row.get("OffsetTimeOriginal") or ""
    try:
        naive = datetime.datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None, ""
    if isinstance(offset, str) and re.match(r"^[+-]\d{2}:\d{2}$", offset):
        sign = 1 if offset[0] == "+" else -1
        delta = datetime.timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
        return naive.replace(tzinfo=datetime.timezone(sign * delta)), offset
    return naive.replace(tzinfo=zone), ""


def _from_filename(name: str, zone: ZoneInfo) -> datetime.datetime | None:
    for pattern in FILENAME_DATES:
        m = pattern.search(name)
        if not m:
            continue
        parts = [int(x) for x in m.groups()]
        y, mo, d = parts[0], parts[1], parts[2]
        h, mi, s = (parts[3], parts[4], parts[5]) if len(parts) > 3 else (0, 0, 0)
        if not (1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31) or _is_epoch(y, mo):
            continue
        try:
            return datetime.datetime(y, mo, d, h, mi, s, tzinfo=zone)
        except ValueError:
            continue
    return None


# how much each answer is worth, so a better one can replace a worse one and
# never the other way round
RANK = {TIME_EXIF: 5, TIME_FILENAME: 4, TIME_PATH: 3, TIME_PEOPLE: 2,
        TIME_FILE_MTIME: 1, TIME_NONE: 0}


COUNTER = {TIME_EXIF: "from_exif", TIME_FILENAME: "from_filename", TIME_PATH: "from_path",
           TIME_FILE_MTIME: "from_mtime", TIME_NONE: "undated"}


async def read(rescan: bool) -> None:
    state = job.state
    async with SessionLocal() as session:
        config = await current_runtime()
        zone = ZoneInfo(config.get("photos_timezone") or "Europe/Zagreb")

        query = select(Photo).options(selectinload(Photo.files))
        if not rescan:
            query = query.where(Photo.taken_source == TIME_NONE)
        photos = (await session.execute(query)).scalars().all()
        state["total"] = len(photos)
        state["phase"] = "reading"

        for start in range(0, len(photos), BATCH):
            chunk = photos[start:start + BATCH]
            primary = _primary_paths(chunk)
            if primary:
                state["current"] = Path(next(iter(primary))).name
                rows = await asyncio.to_thread(_exiftool, list(primary))
                for path, photo in primary.items():
                    row = rows.get(path, {})
                    source = _date(photo, row, zone)
                    _describe(photo, row)
                    state[COUNTER[source]] += 1
                    if source != TIME_NONE:
                        state["dated"] += 1
                await session.commit()
            state["processed"] += len(chunk)

        state["phase"] = "pairing"
        state["paired"] = await _pair_live(session)
        await session.commit()


def _primary_paths(photos) -> dict[str, Photo]:
    """One path per photograph is enough to read the content's own metadata;
    the rest of its files are the same bytes."""
    primary = {}
    for photo in photos:
        live = [f.path for f in photo.files if f.state == FILE_PRESENT]
        if live:
            primary[live[0]] = photo
    return primary


def _date(photo: Photo, row: dict, zone: ZoneInfo) -> str:
    """The best answer to when the photograph was taken, kept only if it is at
    least as good as the one the photograph already has."""
    taken, offset = _from_exif(row, zone)
    source = TIME_EXIF
    if taken is None:
        taken, source = _from_names(photo, zone), TIME_FILENAME
    if taken is None:
        taken, source = _from_folders(photo, zone), TIME_PATH
    if taken is None:
        taken, source = _from_mtimes(photo, zone), TIME_FILE_MTIME
    if taken is None:
        source = TIME_NONE
    if RANK[source] >= RANK[photo.taken_source]:
        photo.taken_at = taken
        photo.taken_offset = offset
        photo.taken_source = source
    return source


def _from_names(photo: Photo, zone: ZoneInfo) -> datetime.datetime | None:
    # every path this photograph is at gets a say, and the earliest wins: a
    # copy is made after the original
    named = (_from_filename(Path(f.path).name, zone) for f in photo.files)
    return min((t for t in named if t), default=None)


def _from_folders(photo: Photo, zone: ZoneInfo) -> datetime.datetime | None:
    months = []
    for f in photo.files:
        m = PATH_MONTH.search(f.path)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and not _is_epoch(y, mo):
            months.append(datetime.datetime(y, mo, 1, tzinfo=zone))
    return min(months, default=None)


def _from_mtimes(photo: Photo, zone: ZoneInfo) -> datetime.datetime | None:
    stamps = [f.mtime_ns for f in photo.files if f.mtime_ns]
    if not stamps:
        return None
    return datetime.datetime.fromtimestamp(
        min(stamps) / 1_000_000_000, tz=datetime.UTC).astimezone(zone)


def _describe(photo: Photo, row: dict) -> None:
    """The camera, the shape, the place and the exposure, as far as the file
    says; what it does not say is left as it was."""
    make, model = row.get("Make"), row.get("Model")
    photo.device_make = str(make)[:64] if make else photo.device_make
    photo.device_model = str(model)[:64] if model else photo.device_model
    w, h = row.get("ImageWidth"), row.get("ImageHeight")
    if isinstance(w, int) and isinstance(h, int):
        sideways = _on_its_side(row) != (photo.turn in (90, 270))
        photo.pixel_w, photo.pixel_h = (h, w) if sideways else (w, h)

    lat, lon = _number(row.get("GPSLatitude")), _number(row.get("GPSLongitude"))
    # both or neither: half a coordinate is not a place, and a zero that
    # arrived alone is a camera with no fix rather than a photograph taken off
    # the coast of Ghana
    if lat is not None and lon is not None and (lat or lon):
        photo.latitude, photo.longitude = lat, lon
        photo.altitude = _number(row.get("GPSAltitude"))
    lens = row.get("LensModel") or row.get("LensID")
    if lens:
        photo.lens = str(lens)[:96]
    photo.focal_mm = _number(row.get("FocalLength")) or photo.focal_mm
    photo.aperture = _number(row.get("FNumber")) or photo.aperture
    iso = row.get("ISO")
    if isinstance(iso, (int, float)):
        photo.iso = int(iso)
    shutter = row.get("ExposureTime")
    if shutter is not None:
        photo.shutter = str(shutter)[:24]
    photo.live_token = _live_token(row)


def _live_token(row: dict) -> str:
    token = row.get("ContentIdentifier") or row.get("MediaGroupUUID")
    return token[:64] if isinstance(token, str) else ""


async def _pair_live(session) -> int:
    """Join the two halves of a Live Photo by the identifier Apple puts in both.

    Not by name and not by timestamp: a burst shares both, and a photograph
    wrongly given somebody else's three seconds of video is worse than one with
    no video at all. The identifier is kept on the row once read, so a photograph
    is opened for it once and never again."""
    unread = (await session.execute(
        select(Photo).options(selectinload(Photo.files))
        .where(Photo.live_pair_id.is_(None), Photo.live_token.is_(None)))).scalars().all()
    for start in range(0, len(unread), BATCH):
        chunk = unread[start:start + BATCH]
        paths = _primary_paths(chunk)
        found = await asyncio.to_thread(_exiftool, list(paths)) if paths else {}
        for path, photo in paths.items():
            photo.live_token = _live_token(found.get(path, {}))
        await session.commit()

    groups = (await session.execute(
        select(Photo.live_token, Photo.id, Photo.kind)
        .where(Photo.live_pair_id.is_(None), Photo.live_token != "")
        .order_by(Photo.live_token))).all()
    by_token: dict[str, list[tuple[int, str]]] = {}
    for token, photo_id, kind in groups:
        by_token.setdefault(token, []).append((photo_id, kind))

    paired = 0
    for group in by_token.values():
        stills = [pid for pid, kind in group if kind == "image"]
        clips = [pid for pid, kind in group if kind == "video"]
        if len(stills) != 1 or len(clips) != 1:
            continue
        await session.execute(update(Photo).where(Photo.id == stills[0])
                              .values(live_pair_id=clips[0]))
        await session.execute(update(Photo).where(Photo.id == clips[0])
                              .values(live_pair_id=stills[0]))
        paired += 1
    return paired
