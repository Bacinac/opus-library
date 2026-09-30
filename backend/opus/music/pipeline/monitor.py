"""Followed artists: their next record is wanted as soon as it is released."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from opus import schedule
from opus.music import landing
from opus.config import settings
from opus.db import SessionLocal
from opus.music.metadata import discography
from opus.models import Artist, MusicFile, Release, ReleaseStatus, Track
from opus.music.pipeline import grab

log = logging.getLogger("opus.music.pipeline")


async def monitor_loop():
    """Following an artist means their next record arrives on its own."""
    await schedule.every("music.monitor", timedelta(seconds=settings.monitor_interval_seconds),
                         _monitor_round)


async def _monitor_round():
    try:
        await _monitor_pass()
    except Exception:
        log.exception("monitor pass failed")
    try:
        await landing.sweep_copies()
    except Exception:
        log.exception("landing sweep of the shelf's copies failed")


async def _monitor_pass():
    """Re-read every followed artist's discography and take what is new.

    New means newly *released*, not newly noticed. A discography sync can turn
    up thirty albums at once — a catalogue page that had been thin, an identity
    only just resolved — and none of that is reason to fetch a back catalogue
    nobody asked for. Only a record that also came out recently is wanted, so
    the first pass over a settled library takes nothing at all.

    A recent record whose search found nothing is asked about again on every
    pass while it is still recent: a release is rarely posted anywhere on the
    day it comes out."""
    started = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        artist_ids = list((await session.execute(
            select(Artist.id).where(Artist.monitored.is_(True))
        )).scalars())

    for artist_id in artist_ids:
        try:
            await discography.sync_artist(artist_id)
        except Exception as exc:
            log.warning("monitor: discography sync failed for artist %s: %s", artist_id, exc)

    release_ids = await _want_new_releases(started)
    if release_ids:
        grab.spawn_grab_bulk(release_ids)


async def _want_new_releases(started: datetime) -> list[int]:
    cutoff = (started - timedelta(days=settings.monitor_new_release_days)).strftime("%Y-%m-%d")
    held = (select(MusicFile.id)
            .join(Track, Track.id == MusicFile.track_id)
            .where(Track.release_id == Release.id).exists())
    async with SessionLocal() as session:
        fresh = list((await session.execute(
            select(Release)
            .join(Artist, Artist.id == Release.artist_id)
            .options(selectinload(Release.artist))
            .where(Artist.monitored.is_(True),
                   Release.created_at >= started,
                   Release.status == ReleaseStatus.NONE,
                   Release.release_date >= cutoff)
        )).scalars())
        unfound = list((await session.execute(
            select(Release)
            .join(Artist, Artist.id == Release.artist_id)
            .options(selectinload(Release.artist))
            .where(Artist.monitored.is_(True),
                   Release.created_at < started,
                   Release.status == ReleaseStatus.FAILED,
                   Release.release_date >= cutoff,
                   ~held)
        )).scalars())
        for release in fresh + unfound:
            release.status = ReleaseStatus.WANTED
            log.info("monitor: wanted %s — %s (%s)%s",
                     release.artist.name, release.title, release.release_date,
                     "" if release in fresh else ", asked again")
        await session.commit()
    return [r.id for r in fresh + unfound]
