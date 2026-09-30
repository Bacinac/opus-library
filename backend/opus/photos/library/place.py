"""Moving an offered picture out of the waiting room and into its year.

A picture from somebody's phone arrives knowing almost nothing about itself. The
date a phone puts on a file is the date it was last copied about — through a
chat, off a card, out of a backup — and the holiday in the photograph may have
been six years ago. So an offered picture is not filed on arrival. It waits in
one folder until the ordinary pass has read its EXIF, and only then does it move
to the year it turned out to belong to.

Which is why this reads `taken_at` rather than working it out: dating a
photograph is a policy with four fallbacks and a timezone in it, it lives in
opus.photos.library.metadata, and a second opinion here would be a second
policy — quietly disagreeing with the first about exactly the awkward cases the
first was written for.

The move stays inside one filesystem, so it costs nothing and copies nothing.
What it must not do is look like a deletion: the row is updated in the
same breath, because a pass that found the old path gone and the new path new
would tombstone a photograph and then re-read it from the beginning."""

import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from opus.db import SessionLocal
from opus.models import FILE_PRESENT, Photo, PhotoFile
from opus.photos.library import offers
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

# enough that a large evening of offerings finishes in one pass, small enough
# that nothing else waits on the transaction
BATCH = 200


def _where(when, zone: str) -> str:
    """The folder a picture belongs in: the year and month a person would say it
    is from — theirs, not UTC's. A photograph taken at half past midnight on New
    Year's Eve belongs to the year the household was in at the time.

    Year AND month, because that is the shape of this tree — thirty-seven
    thousand of its photographs are filed that way and only the ones that came
    through the waiting room were not. A folder of one convention beside a
    folder of another is not a tidiness question: it is the tree no longer
    saying one thing.
    """
    here = when.astimezone(ZoneInfo(zone))
    return f"{here.year:04d}/{here.month:02d}"


async def settle() -> dict:
    """Move everything in the waiting room that the catalogue can now date.

    One picture per transaction, and that is the whole of what this docstring
    needs to say. It used to move two hundred files and then write two hundred
    rows in one commit: a single unique-path collision at the end raised, the
    commit rolled back, and two hundred pictures sat on the disk at paths the
    catalogue had never heard of. The catalogue said "inbox", the tree said
    "2022", and nothing but a full scan could tell that they disagreed.

    A move that cannot be recorded is undone, so the two never part company.
    """
    moved, refused = 0, 0
    async with SessionLocal() as session:
        config = await current_runtime()
        zone = config.get("photos_timezone") or "UTC"
        read_root = Path(config.get("photos_dir"))
        write_root = offers.writable(config)
        # asked of the catalogue, so under the name the catalogue keeps
        seen = str(offers.waiting_seen(config))
        waiting_here = (PhotoFile.path.like(f"{seen}/%"), PhotoFile.state == FILE_PRESENT)
        swept = offers.sweep(config)

        # not dated yet means the metadata pass has not reached it. It stays
        # where it is, which is inside the tree, so nothing is lost by waiting —
        # an undated picture is in the library already, just not yet in a year.
        waiting = (await session.execute(
            select(func.count()).select_from(PhotoFile)
            .join(Photo, Photo.id == PhotoFile.photo_id)
            .where(*waiting_here, Photo.taken_at.is_(None)))).scalar_one()
        rows = (await session.execute(
            select(PhotoFile.id, PhotoFile.path, Photo.taken_at, Photo.checksum)
            .join(Photo, Photo.id == PhotoFile.photo_id)
            .where(*waiting_here, Photo.taken_at.is_not(None))
            .order_by(PhotoFile.id)
            .limit(BATCH))).all()

        for file_id, path, taken_at, checksum in rows:
            where = _where(taken_at, zone)
            (write_root / where).mkdir(parents=True, exist_ok=True)
            name = Path(path).name
            source = offers.waiting_room(config) / name
            try:
                target = await offers.land(session, source, write_root / where,
                                           read_root / where, name, checksum)
                source.unlink()
            except OSError as why:
                log.warning("could not place %s: %s", path, why)
                continue

            stat = target.stat()
            # the catalogue only ever speaks the read-only name, so the path in
            # the row is the path the next pass will go looking for
            await session.execute(
                update(PhotoFile).where(PhotoFile.id == file_id)
                .values(path=str(read_root / where / target.name),
                        dev=stat.st_dev, inode=stat.st_ino,
                        mtime_ns=stat.st_mtime_ns, byte_size=stat.st_size))
            try:
                await session.commit()
            except Exception as why:
                # the row could not be written, so the file must not stay moved:
                # a picture the catalogue cannot find is worse than one that has
                # not been filed yet
                await session.rollback()
                try:
                    os.replace(target, source)
                except OSError:
                    log.error("placed %s at %s and could neither record nor "
                              "undo it", path, target)
                else:
                    log.warning("could not record %s: %s", target, why)
                refused += 1
                continue
            moved += 1
    return {"placed": moved, "undated": waiting, "refused": refused, "swept": swept}
