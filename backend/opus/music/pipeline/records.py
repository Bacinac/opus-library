"""What every record on the shelf is — its paragraph and its facts — asked
for a batch at a time."""

import asyncio
import logging

from sqlalchemy import and_ as sa_and, or_ as sa_or, select

from opus.db import SessionLocal
from opus.music.metadata import tracklists
from opus.music.metadata.deezer import DeezerClient
from opus.music.metadata import wikidata as wd
from opus.models import Release, ReleaseStatus

log = logging.getLogger("opus.music.pipeline")


# What a record IS, for the whole shelf rather than for whichever record
# somebody happened to open: the paragraph from the album's own Wikipedia
# article (through its Wikidata sitelink, or by searching Wikipedia for the
# record by name), and the facts — who put it out and what kind of music it
# is — from Deezer, in the language the catalogue is read in. Both are
# somebody else's server, so this goes at a walking pace and stops when there
# is nothing left to ask.
RECORDS_BATCH = 40
RECORDS_PACE = 1.2
RECORDS_IDLE = 6 * 3600


async def records_loop():
    """Fill in what every record on the shelf is, a batch at a time."""
    await asyncio.sleep(90)
    while True:
        asked = 0
        try:
            asked = await _records_pass()
        except Exception:
            log.exception("record backfill iteration failed")
        # straight on while there is work, and a long wait once the shelf is
        # answered — new records arrive slowly
        await asyncio.sleep(5 if asked else RECORDS_IDLE)


async def _records_pass() -> int:
    """One batch of records that have never been asked what they are."""
    async with SessionLocal() as session:
        releases = list((await session.execute(
            select(Release).where(
                Release.status == ReleaseStatus.COMPLETE,
                sa_or(
                    Release.description.is_(None),
                    sa_and(Release.label.is_(None), Release.deezer_id.is_not(None)),
                ),
            ).order_by(Release.id).limit(RECORDS_BATCH)
        )).scalars())
        if not releases:
            return 0
        before = [(bool(r.description), bool(r.label)) for r in releases]
        wiki = wd.WikidataClient()
        deezer = DeezerClient()
        try:
            for release in releases:
                try:
                    await tracklists.ensure_description(release, client=wiki)
                except Exception as exc:
                    log.warning("description backfill failed for release %s: %s", release.id, exc)
                try:
                    await tracklists.ensure_facts(release, deezer=deezer)
                except Exception as exc:
                    log.warning("facts backfill failed for release %s: %s", release.id, exc)
                await asyncio.sleep(RECORDS_PACE)
            await session.commit()
        finally:
            await wiki.close()
            await deezer.close()
        gained = sum(1 for was, r in zip(before, releases)
                     if (bool(r.description), bool(r.label)) != was)
        log.info("records: asked about %s, learned about %s",
                 len(releases), gained)
        # What was LEARNED, not what was asked. A release the sources cannot
        # answer for stays in the query for ever, so counting the asking kept
        # this loop at five seconds a turn indefinitely: an API called for
        # nothing every six seconds, and an event loop busy enough that the
        # photographs beside it never got a pass.
        return gained
