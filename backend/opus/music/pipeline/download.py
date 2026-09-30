"""Watching the downloads in flight and handing each finished one to the importer."""

import asyncio
import logging

from sqlalchemy import select

from opus import acquire
from opus.music.channels import enabled_channels
from opus.acquire import AcquireError
from opus.config import settings
from opus.db import SessionLocal
from opus.models import MusicDownload, MusicDownloadStatus
from opus.settings_store import current_runtime
from opus.music.pipeline import grab, importer, state

log = logging.getLogger("opus.music.pipeline")


async def poll_downloads_loop():
    try:
        await grab.settle_interrupted()
    except Exception:
        log.exception("settling the searches the last run left open failed")
    while True:
        try:
            await _poll_once()
        except Exception:
            log.exception("download poller iteration failed")
        await asyncio.sleep(settings.poll_interval_seconds)


async def _poll_once():
    async with SessionLocal() as session:
        result = await session.execute(
            select(MusicDownload).where(MusicDownload.status == MusicDownloadStatus.DOWNLOADING)
        )
        active = result.scalars().all()
        # a row left in IMPORTING is one whose import died mid-flight — a
        # restart, or a raise. Nothing else ever looks at it again, so it
        # would claim 'Importing…' forever; retry it here and let the import
        # either finish or fail visibly.
        stranded = (await session.execute(
            select(MusicDownload.id).where(MusicDownload.status == MusicDownloadStatus.IMPORTING)
        )).scalars().all()
        config = await current_runtime()

    for download_id in stranded:
        if download_id in state.importing:
            continue
        log.warning("download %s was stranded mid-import — retrying", download_id)
        try:
            await importer.import_download(download_id)
        except Exception:
            # one unimportable row must not hold up every other stranded one
            log.exception("stranded import %s failed again", download_id)

    channels = {c.name: c for c in enabled_channels(config)}

    for download in active:
        channel = channels.get(download.channel)
        if channel is None:
            await grab.fail(download.id, f"channel {download.channel} is not enabled")
            continue
        try:
            status = await channel.job_status(download.job_ref)
        except AcquireError as exc:
            log.error("status poll failed for download %s: %s", download.id, exc)
            continue
        except Exception:
            # a channel that breaks in some way it did not anticipate must cost
            # its own row and nothing else — the same rule the stranded imports
            # above follow, and without it one malformed job_ref stops every
            # other download from being polled at all
            log.exception("status poll crashed for download %s", download.id)
            continue

        state.live[download.id] = {
            "progress": status.progress,
            "detail": status.detail,
            "files": status.files,
        }
        if status.state == "unknown":
            if acquire.unaccounted_for(status, download.created_at):
                await grab.fail(download.id,
                            f"{download.channel} lost the job: {status.detail}")
            continue
        if status.state == "failed":
            await grab.fail(download.id, status.detail)
        elif status.state == "complete":
            if status.directory and download.job_ref.get("directory") != status.directory:
                async with SessionLocal() as session:
                    row = await session.get(MusicDownload, download.id)
                    row.job_ref = {**row.job_ref, "directory": status.directory}
                    await session.commit()
            await importer.import_download(download.id)
