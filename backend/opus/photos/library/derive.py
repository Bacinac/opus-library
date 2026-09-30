"""Making a photograph visible, without touching the photograph.

Three artefacts come out of one decode. Two go on disk and one goes in the row,
and which is which was decided by measurement rather than taste:

  thumbhash  21 bytes in Postgres. Draws a blurred impression in the right
             colours, and because it sits in the row it arrives with the query
             that lists the photographs — so a grid has something to draw before
             any file has left the disk.
  tile       350 px, JPEG. The grid fetches sixty of these at once and decodes
             every one, so what matters is DECODE: 1.17 ms for JPEG against
             4.6-5.2 ms for WebP or AVIF. Sixty tiles is 70 ms rather than 300.
  preview    2048 px, AVIF. One file, fetched over the network, decoded once —
             so what matters is BYTES, and AVIF is 39 % under JPEG at equal
             quality. This is the opposite way round from Immich, on purpose.

Nothing here writes under the photo tree. The originals are mounted read-only
and the derivatives have a volume of their own, which is what makes "the photo
half never writes to the library" a property of the installation rather than a
promise in the code.

The derivative store is a CACHE and says so, in two dialects: CACHEDIR.TAG for
restic, .pxarexclude for the Proxmox backup client. Both are rewritten at every
start, so a restore that drops them heals itself.
"""

import asyncio
import datetime
import logging
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from opus.db import SessionLocal
from opus.models import FILE_PRESENT, Photo, PhotoIntegrityEvent
from opus.passes import Pass, Refused
from opus.photos.library import heif
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

# Bump when the recipe changes. It is part of the filename, so a new generation
# is a new file beside the old one — never an overwrite that leaves half a
# picture if the pass dies — and reverting is a number, not a sweep.
GENERATION = 1

TILE_PX = 350
# Past the largest phone sensor (200 MP) with room to spare. A file declaring more
# than this is a decompression bomb, and decoding it would take the box down.
MOST_PIXELS = 300_000_000
PREVIEW_PX = 2048
HASH_PX = 100

# effort=2 and not libvips' default of 4: the default buys 26 % of the bytes for
# 33x the time, which is the whole of the "AVIF takes 32 hours" folklore.
# Never effort=1 — with the aom plugin it is measurably slower AND larger than 0.
# keep=icc, not strip=false: the colour profile has to travel or the picture is
# shown in the wrong colours, but nothing else does. A tile is served to a
# browser, and the original's EXIF carries where the photograph was taken and
# which camera took it — neither of which anyone asked to publish. It is also
# smaller, though that is the lesser reason.
PREVIEW_SAVE = "[compression=av1,encoder=aom,effort=2,Q=63,keep=icc]"
TILE_SAVE = "[Q=84,interlace,optimize_coding,keep=icc]"

CACHEDIR_TAG = (
    "Signature: 8a477f597d28d172789f06886806bc55\n"
    "# OPUS · Library photo derivatives. Rebuilt from the originals and the\n"
    "# catalogue; carrying them offsite costs space for something that can be\n"
    "# made again, and re-encoding changes every byte so nothing dedupes.\n"
)

# libvips threads inside a single image; we want the opposite. Many independent
# photographs parallelise better one-per-worker than one image split four ways,
# and letting both happen at once just makes them fight for the same cores.
os.environ.setdefault("VIPS_CONCURRENCY", "1")

job = Pass("derivatives", total=0, processed=0, made=0, skipped=0, failed=0,
           underivable=0, bytes=0, current="", workers=0)


def store_root(config) -> Path:
    return Path(config.get("photos_derivatives_dir") or "/derivatives")


def prepare_store(root: Path) -> None:
    """Make the store, and make it declare what it is.

    Two dialects because /mnt/docker is captured four different ways and only
    restic honours CACHEDIR.TAG; the Proxmox client reads .pxarexclude and the
    ZFS snapshots cannot be told anything at all."""
    for sub in ("tile", "preview", ".incoming"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "CACHEDIR.TAG").write_text(CACHEDIR_TAG)
    (root / ".pxarexclude").write_text("*\n")


def paths_for(root: Path, checksum: bytes) -> tuple[Path, Path]:
    """Where a photograph's derivatives live, addressed by what it contains.

    One shard level of 256 rather than Immich's four hex characters: on this box
    that produced 35,012 directories holding 43,382 assets — 1.24 files each,
    sized for a corpus a hundred times larger than the one it has.

    Names stay under 50 bytes so ZFS keeps these directories in microzap:
    <40 hex>-g1.avif is 48. That is also why the key is the SHA-1 already in the
    row and the generation is a single hex digit."""
    hexed = checksum.hex()
    shard = hexed[:2]
    stem = f"{hexed}-g{GENERATION:x}"
    return (root / "tile" / shard / f"{stem}.jpg",
            root / "preview" / shard / f"{stem}.avif")


# Where in a recording to take the picture from. Not the first frame: a phone
# spends the first moment opening its lens, and half the library would be
# thumbnails of a grey blur. A second in, or the middle of anything shorter.
POSTER_AT = 1.0


def _poster(source: Path, into: Path) -> Path:
    """One frame out of a recording, written beside it, so everything below can
    go on believing it was handed a picture.

    A recording is a photograph in the shelf like any other — it has a date, a
    place and people in it — and it was the only kind with nothing to show for
    itself: five hundred and forty-eight blank squares in the grid."""
    import subprocess

    when = POSTER_AT
    told = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(source)],
        capture_output=True, text=True, timeout=30)
    try:
        length = float(told.stdout.strip())
        if length and length < 2 * POSTER_AT:
            when = length / 2
    except ValueError:
        pass

    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{when:.2f}",
         "-i", str(source), "-frames:v", "1", "-q:v", "2", str(into)],
        capture_output=True, text=True, timeout=120)
    if done.returncode != 0 or not into.exists() or into.stat().st_size == 0:
        raise RuntimeError(f"no frame could be taken: {done.stderr.strip()[:200]}")
    return into


def _write(source: Path, tile: Path, preview: Path, incoming: Path,
           moving: bool = False, turn: int = 0) -> tuple[int, bytes | None]:
    """One decode, three encodes. Returns bytes written and the thumbhash.

    Written to .incoming and renamed into place, so a pass that dies never
    leaves a half-written picture where a whole one is expected."""
    import pyvips
    from thumbhash import rgba_to_thumb_hash

    # A recording is turned into one picture first, and everything after this is
    # the same for both kinds.
    frame = None
    if moving:
        # named after what it is for, not after the folder it is in: `incoming`
        # is one directory shared by every worker, and four of them writing
        # `.incoming.poster.jpg` at once produced four half-frames and three
        # decoders reading somebody else's
        frame = incoming / f"{tile.stem}.poster.jpg"
        source = _poster(source, frame)

    # Before the decoder is given the chance to be creative. libheif 1.21 and
    # later fill a HEIC's absent tiles with invented content and report success,
    # so "it decoded" stops being evidence that the photograph is there.
    hole = heif.incomplete(source)
    if hole:
        raise heif.Incomplete(hole)
    header = pyvips.Image.new_from_file(str(source))
    if header.width * header.height > MOST_PIXELS:
        raise RuntimeError(f"{header.width}x{header.height} is more picture than a "
                           f"photograph holds; not decoded")

    # sequential access lets libvips stream rather than hold the whole image
    image = pyvips.Image.thumbnail(str(source), PREVIEW_PX, height=PREVIEW_PX,
                                   size=pyvips.enums.Size.DOWN)
    # Mandatory, not hygiene: a lazy libvips pipeline can be read exactly once,
    # and deriving the tile from the same unmaterialised image dies mid-save.
    if turn:
        image = image.rot(f"d{turn}")
    image = image.copy_memory()

    tile.parent.mkdir(parents=True, exist_ok=True)
    preview.parent.mkdir(parents=True, exist_ok=True)
    # The extension has to stay last: libvips picks the encoder from it, so a
    # ".avif.part" is a file in no format at all. The names are already unique —
    # they are content-addressed — so the staging directory is enough.
    tmp_preview = incoming / preview.name
    tmp_tile = incoming / tile.name

    image.write_to_file(str(tmp_preview) + PREVIEW_SAVE)
    # An encoder that is not installed fails AND leaves a zero-byte file, which
    # a pass that only checks the return code will record 44,701 times.
    if tmp_preview.stat().st_size == 0:
        raise RuntimeError("AVIF encoder wrote nothing — is libheif-plugin-aomenc installed?")

    tile_image = image.thumbnail_image(TILE_PX, height=TILE_PX,
                                       size=pyvips.enums.Size.DOWN)
    tile_image.write_to_file(str(tmp_tile) + TILE_SAVE)
    if tmp_tile.stat().st_size == 0:
        raise RuntimeError("JPEG encoder wrote nothing")

    # thumbhash wants straight RGBA; anything else in the pipeline (CMYK from a
    # scanner, greyscale, a stray alpha) has to be made into that first
    small = image.thumbnail_image(HASH_PX, height=HASH_PX, size=pyvips.enums.Size.DOWN)
    small = small.colourspace(pyvips.enums.Interpretation.SRGB)
    if small.bands == 3:
        small = small.bandjoin(255)
    elif small.bands > 4:
        small = small.extract_band(0, n=4)
    # write_to_memory returns a memoryview here, and listing one yields chunks
    # rather than the plain integers thumbhash indexes into
    digest = rgba_to_thumb_hash(small.width, small.height,
                                list(bytes(small.write_to_memory())))

    written = tmp_preview.stat().st_size + tmp_tile.stat().st_size
    os.replace(tmp_preview, preview)
    os.replace(tmp_tile, tile)
    if frame is not None:
        frame.unlink(missing_ok=True)
    return written, bytes(digest)


async def make(regenerate: bool, retry_failed: bool) -> None:
    state = job.state
    async with SessionLocal() as session:
        config = await current_runtime()
        root = store_root(config)
        try:
            await asyncio.to_thread(prepare_store, root)
        except OSError as exc:
            raise Refused(f"the derivative store at {root} cannot be written: {exc}") from exc

        photos = await _to_derive(session, regenerate, retry_failed)
        state["total"] = len(photos)
        state["phase"] = "deriving"

        incoming = root / ".incoming"
        workers = max(1, int(config.float("photos_derive_workers")))
        state["workers"] = workers

        # A batch of photographs is encoded at once and only then written to
        # the database: pyvips drops the GIL inside libvips, so the threads
        # genuinely run side by side, while the session stays on one thread
        # because an async session is not safe to share.
        for start in range(0, len(photos), workers):
            if job.stopping:
                return
            jobs = _encodings(photos[start:start + workers], root, incoming)
            if not jobs:
                continue
            state["current"] = jobs[0][1].name
            done = await asyncio.gather(*(j[2] for j in jobs), return_exceptions=True)
            for (photo, source, _), outcome in zip(jobs, done):
                _note(session, photo, source, outcome)
                state["processed"] += 1
            await session.commit()


async def _to_derive(session, regenerate: bool, retry_failed: bool) -> list[Photo]:
    # Recordings included. They were left out because there was nothing
    # to decode them with; there is now, and until there was, five
    # hundred and forty-eight of them sat in the shelf as blank squares.
    query = select(Photo).options(selectinload(Photo.files))
    if not regenerate:
        query = query.where(Photo.derived_gen != GENERATION)
        # A photograph this recipe has already failed on is not read
        # again by every subsequent pass: the bytes have not changed and
        # neither has the decoder, so the only thing a retry buys is the
        # same error and two more seconds. `retry_failed` is how you ask
        # for it on purpose — after a newer libheif, say.
        if not retry_failed:
            query = query.where(Photo.derive_failed_gen != GENERATION)
    return (await session.execute(query)).scalars().all()


def _encodings(batch: list[Photo], root: Path, incoming: Path) -> list[tuple]:
    """One encoding per photograph that still has a file to read; one that has
    none is counted as skipped."""
    jobs = []
    for photo in batch:
        live = [f for f in photo.files if f.state == FILE_PRESENT]
        if not live:
            job.state["skipped"] += 1
            job.state["processed"] += 1
            continue
        source = Path(live[0].path)
        tile, preview = paths_for(root, photo.checksum)
        jobs.append((photo, source,
                     asyncio.to_thread(_write, source, tile, preview,
                                       incoming, photo.kind == "video",
                                       photo.turn)))
    return jobs


def _note(session, photo: Photo, source: Path, outcome):
    state = job.state
    if isinstance(outcome, BaseException):
        state["failed"] += 1
        # one line, flattened: a libvips error is a paragraph
        # and this has to fit in a column and read in a table
        why = " ".join(str(outcome).split())[:200]
        log.warning("deriving failed for %s: %s", source, why)
        if photo.derive_failed_gen != GENERATION:
            state["underivable"] += 1
            session.add(PhotoIntegrityEvent(
                kind="underivable", path=str(source),
                detail={"error": why, "generation": GENERATION}))
        photo.derive_failed_gen = GENERATION
        photo.derive_error = why
        return
    written, digest = outcome
    photo.thumbhash = digest
    photo.derived_at = datetime.datetime.now(datetime.UTC)
    photo.derived_gen = GENERATION
    # a photograph that reads now is not a photograph that
    # failed once — the file may have been replaced by an
    # intact copy of the same picture
    photo.derive_failed_gen = 0
    photo.derive_error = ""
    state["made"] += 1
    state["bytes"] += written
