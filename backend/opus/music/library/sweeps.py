"""Post-scan sweeps: reconcile the DB with what the scan actually saw. Each
one repairs a class of drift a full pass leaves behind — rows whose file is
gone, COMPLETE releases holding nothing, husks nothing points at. They run only
after a scan COMPLETES: against a half-walked tree the same logic reads as mass
deletion."""

import logging
from pathlib import Path

from sqlalchemy import func, select

from opus.db import SessionLocal
from opus.music.library import inventory
from opus.models import (
    RELEASE_SOURCE_IDS,
    Artist,
    MusicDownload,
    FolderScanCache,
    Image,
    MusicFile,
    Release,
    ReleaseStatus,
    Track,
)

log = logging.getLogger("opus.libimport")


async def sweep_orphans():
    """Rows nothing points at any more. A release whose last source id was
    freed (a mis-merge un-glue hands the master to the real album) used to
    linger forever as an empty husk — one per scan. Images are a polymorphic
    table with no FK, so a deleted artist/release strands its candidates, and
    a deleted folder strands its cached verdict."""
    async with SessionLocal() as session:
        husks = (await session.execute(
            select(Release).where(
                *(getattr(Release, column).is_(None) for column in RELEASE_SOURCE_IDS),
                Release.status == ReleaseStatus.NONE,
                ~select(MusicFile.id)
                .join(Track, Track.id == MusicFile.track_id)
                .where(Track.release_id == Release.id).exists(),
                ~select(MusicDownload.id)
                .where(MusicDownload.release_id == Release.id).exists(),
            )
        )).scalars().all()
        for release in husks:
            await session.delete(release)

        images = (await session.execute(
            select(Image).where(
                ((Image.entity_type == "release")
                 & ~select(Release.id).where(Release.id == Image.entity_id).exists())
                | ((Image.entity_type == "artist")
                   & ~select(Artist.id).where(Artist.id == Image.entity_id).exists())
            )
        )).scalars().all()
        for image in images:
            await session.delete(image)

        stale = (await session.execute(
            select(FolderScanCache).where(
                ~select(MusicFile.id)
                .where(MusicFile.path.like(func.concat("%/", FolderScanCache.folder, "/%")))
                .exists()
            )
        )).scalars().all()
        for row in stale:
            await session.delete(row)
        await session.commit()
    if husks or images or stale:
        log.info("orphan sweep: %d empty releases, %d stranded images, "
                 "%d stale folder verdicts", len(husks), len(images), len(stale))


async def sweep_missing_files():
    """After a FULL scan, drop file rows whose file vanished from disk."""
    async with SessionLocal() as session:
        rows = (await session.execute(select(MusicFile))).scalars().all()
        removed = 0
        for row in rows:
            if row.path not in inventory.seen_paths and not Path(row.path).exists():
                await session.delete(row)
                removed += 1
        await session.commit()
    if removed:
        log.info("library sweep: removed %d rows for vanished files", removed)


async def sweep_status_truth():
    """COMPLETE must mean 'holds files'. Re-links can strand a duplicate row
    (a freed mis-merge, an edition fork) with COMPLETE status and no files —
    it would keep showing 'In library' and block re-adoption forever."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Release).where(Release.status == ReleaseStatus.COMPLETE)
        )).scalars().all()
        demoted = 0
        for release in rows:
            linked = (await session.execute(
                select(func.count()).select_from(MusicFile)
                .join(Track, MusicFile.track_id == Track.id)
                .where(Track.release_id == release.id)
            )).scalar()
            if linked == 0:
                release.status = ReleaseStatus.NONE
                demoted += 1
        await session.commit()
    if demoted:
        log.info("status truth: %d file-less COMPLETE releases demoted", demoted)
