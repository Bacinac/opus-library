"""Sequential per-artist metadata pass: Wikidata enrichment, multi-source
discography sync and artist artwork for every monitored artist, one artist at
a time — friendly to every source's rate limit. Auto-runs after a library
scan; can be started manually from the Library page."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from opus.db import SessionLocal
from opus.passes import Pass
from opus.music.metadata import artwork, discography, enrich
from opus.models import Artist

log = logging.getLogger("opus.libenrich")

job = Pass("library enrich", timed=False, total=0, processed=0, current=None, failed=[])


def start(rescan_after: bool = False, artist_ids: list[int] | None = None,
          force: bool = False) -> bool:
    return job.start(_run, rescan_after, artist_ids, force)


async def _run(rescan_after: bool, artist_ids: list[int] | None, force: bool):
    state = job.state
    async with SessionLocal() as session:
        query = (select(Artist.id, Artist.name)
                 .where(Artist.monitored)
                 .order_by(Artist.name))
        if artist_ids is not None:
            query = query.where(Artist.id.in_(artist_ids))
        elif not force:
            # checkpointed: a restart resumes with the artists still
            # missing a fresh cycle instead of re-walking all of them
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            query = query.where((Artist.enriched_at.is_(None))
                                | (Artist.enriched_at < cutoff))
        artists = (await session.execute(query)).all()
    state["total"] = len(artists)
    log.info("library enrich: %d artists queued", len(artists))

    # a few artists in flight at once: their per-source rate limits
    # interleave instead of summing. Shared compilations can race two
    # workers into the same unique deezer_id — one retry serializes it.
    queue: asyncio.Queue = asyncio.Queue()
    for row in artists:
        queue.put_nowait(row)

    async def _worker():
        while True:
            try:
                artist_id, name = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            state["current"] = name
            for attempt in (1, 2):
                try:
                    # always re-enrich: refreshes bio/relations under
                    # current rules
                    await enrich.enrich_artist(artist_id, chain_discography=False)
                    await discography.sync_artist(artist_id)
                    await artwork.refresh_artist_artwork(artist_id)
                    async with SessionLocal() as session:
                        row = await session.get(Artist, artist_id)
                        row.enriched_at = datetime.now(timezone.utc)
                        await session.commit()
                    break
                except Exception:
                    if attempt == 2:
                        log.exception("library enrich failed for %s (%s)",
                                      name, artist_id)
                        state["failed"].append(name)
            state["processed"] += 1

    await asyncio.gather(*(_worker() for _ in range(3)))
    log.info("library enrich finished: %d artists, %d failed",
             state["processed"], len(state["failed"]))
    if rescan_after:
        # one follow-up scan picks up albums the secondary sources just
        # added (Discogs Jugoton-era etc.); chain_enrich=False stops the
        # scan→enrich→scan cycle there
        from opus.music.library import scan
        scan.start(chain_enrich=False)
