"""What every file's streams are, read once and kept."""

import asyncio
import logging

from sqlalchemy import delete, select

from opus.db import SessionLocal
from opus.models import Chapter, MediaStream, VideoFile
from opus.video.subtitles.probe import probe_file

log = logging.getLogger("opus.video.pipeline")


# What a sound track is beyond the name of its core: a file probed for its core
# alone answers "dca" for DTS-HD Master Audio and "truehd" for TrueHD with
# Atmos, and the screen would draw the mark of the core on the better copy.
# Reading a header is cheap; reading two thousand of them at once is not, so
# this walks them.
PROFILES_BATCH = 40
PROFILES_IDLE = 6 * 3600


async def profiles_loop():
    """Re-read the headers of files whose sound tracks never said what they are."""
    await asyncio.sleep(120)
    while True:
        done = 0
        try:
            done = await _profiles_pass()
        except Exception:
            log.exception("stream profile backfill iteration failed")
        await asyncio.sleep(5 if done else PROFILES_IDLE)


async def _profiles_pass() -> int:
    async with SessionLocal() as session:
        files = list((await session.execute(
            select(VideoFile)
            .join(MediaStream, MediaStream.file_id == VideoFile.id)
            .where(MediaStream.kind == "audio", MediaStream.profile.is_(None))
            .order_by(VideoFile.id)
            .limit(PROFILES_BATCH)
        )).scalars().unique())
        if not files:
            return 0
        read = 0
        for media in files:
            try:
                info = await probe_file(media.path)
            except Exception as exc:
                log.warning("profile backfill: %s could not be read: %s", media.path, exc)
                # nothing to read means nothing to ask again about; the row is
                # left as it is and the next pass would pick it up for ever, so
                # it is marked as asked with an empty profile
                for stream in await media.awaitable_attrs.streams:
                    if stream.kind == "audio" and stream.profile is None:
                        stream.profile = ""
                continue
            await persist_streams(session, media, info)
            # A codec with no profile to give — PCM, and it is not alone — leaves
            # the column null after a perfectly successful read, and the query
            # above then asks about that file again on the next turn, and every
            # turn after. Asked and answered with nothing is the same answer as
            # unreadable, and is written down the same way. Without this the loop
            # sat at five seconds a turn for ever, re-reading the same ten files
            # and keeping the event loop busy enough that the photographs beside
            # it never got a pass.
            for stream in await media.awaitable_attrs.streams:
                if stream.kind == "audio" and stream.profile is None:
                    stream.profile = ""
            read += 1
            # the box this reads from is the box somebody is watching a film on
            await asyncio.sleep(0.2)
        await session.commit()
        log.info("stream profiles: re-read %s of %s files", read, len(files))
        return len(files)


async def persist_streams(session, media: VideoFile, info: dict) -> None:
    """What the file holds, recorded where the file is recorded. Replaced whole
    rather than merged: a re-import is a different file at the same path, and
    half of one inventory beside half of another describes nothing."""
    await session.execute(delete(MediaStream).where(MediaStream.file_id == media.id))
    for stream in info.get("streams") or []:
        session.add(MediaStream(file_id=media.id, **stream))
    await session.execute(delete(Chapter).where(Chapter.file_id == media.id))
    for chapter in info.get("chapters") or []:
        session.add(Chapter(file_id=media.id, **chapter))
