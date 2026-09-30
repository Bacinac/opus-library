"""Watching the downloads in flight and handing each finished one to the
importer."""

import asyncio
import logging

from sqlalchemy import select

from opus import acquire
from opus.acquire import AcquireError
from opus.video.metadata.webvideo import probe_url
from opus.config import settings
from opus.db import SessionLocal
from opus.models import VideoDownload, WebChannel, WebVideo
from opus.settings_store import RuntimeConfig, current_runtime
from opus.video.subtitles.probe import ProbeError
from opus.video.pipeline import grab, importer

log = logging.getLogger("opus.video.pipeline")


_channel_poll_counter = 0
WEB_CHANNEL_POLL_EVERY = 360  # poll loops between web-channel refreshes (~1 h)


async def _refresh_web_channels(session, config: RuntimeConfig) -> None:
    result = await session.execute(select(WebChannel).where(WebChannel.monitored))
    for channel in result.scalars():
        try:
            info = await probe_url(channel.url)
        except AcquireError as exc:
            log.warning("web channel refresh failed for %s: %s", channel.url, exc)
            continue
        for entry in info.get("entries", []) or []:
            ext_id = entry.get("id")
            if not ext_id:
                continue
            existing = await session.execute(
                select(WebVideo).where(WebVideo.external_id == ext_id))
            if existing.scalar_one_or_none() is not None:
                continue
            video = WebVideo(
                channel_id=channel.id, external_id=ext_id,
                url=entry.get("url") or entry.get("webpage_url") or "",
                title=entry.get("title", ""), uploader=channel.title,
                duration_s=int(entry["duration"]) if entry.get("duration") else None,
            )
            session.add(video)
            await session.flush()
            await grab.grab_web_video(session, config, video)
    await session.commit()


async def _run_import(session, config: RuntimeConfig, dl: VideoDownload) -> None:
    """Import one completed download and settle the row either way. The commit
    belongs here, per download, rather than at the end of the sweep: the files
    have already moved by the time it runs, and a batch that is rolled back
    after that leaves the library holding what the catalogue never heard of.

    The id is read before anything can go wrong on purpose: a rollback expires
    every object in the session whatever `expire_on_commit` says, so reaching
    for `dl.id` afterwards is a lazy load — and a lazy load on an async session
    raises inside the handler that was supposed to record the failure. That is
    how a failed import used to leave the row sitting at `downloaded`."""
    download_id, channel, job_ref = dl.id, dl.channel, dl.job_ref
    try:
        await importer.import_download(session, config, dl, dl.job_ref.get("landing") or "")
        await session.commit()
    except importer.ImportFailure as exc:
        detail = str(exc)
    except ProbeError as exc:
        detail = str(exc)
    except Exception as exc:
        log.exception("import failed for download %s", download_id)
        detail = f"import failed: {exc}"
    else:
        if config.bool("cleanup_after_import"):
            try:
                await grab.make_channel(channel, config).drop_job(job_ref)
            except AcquireError as exc:
                log.warning("post-import cleanup failed for download %s: %s", download_id, exc)
        return
    await session.rollback()
    dl = await session.get(VideoDownload, download_id)
    dl.state = "failed"
    dl.detail = detail
    await grab.bury(session, dl)
    await session.commit()
    await grab.try_next_release(session, config, dl)


async def _download_ids(session, states: tuple[str, ...]) -> list[int]:
    """The rows to work through, as ids rather than as objects. A rollback
    anywhere in the sweep expires every object the session is holding, and the
    next attribute read on one of them would be a lazy load that an async
    session cannot serve — so each row is fetched afresh when its turn comes."""
    result = await session.execute(
        select(VideoDownload.id).where(VideoDownload.state.in_(states))
        .order_by(VideoDownload.id))
    return [row_id for (row_id,) in result]


async def _resume_stranded(session, config: RuntimeConfig) -> None:
    """Downloads that reached the disk but never reached the catalogue.

    `downloaded` is the state an item wears while its files are being moved
    into the library, and the poll loop does not watch it — so a process that
    dies inside that window used to leave the item there for good. Sweeping it
    on the way in is what makes an import survive a restart."""
    for download_id in await _download_ids(session, ("downloaded",)):
        dl = await session.get(VideoDownload, download_id)
        log.info("resuming stranded import for download %s (%s)", dl.id, dl.release_title)
        await _run_import(session, config, dl)


async def poll_downloads_loop():
    global _channel_poll_counter
    while True:
        try:
            async with SessionLocal() as session:
                config = await current_runtime()
                await _resume_stranded(session, config)
                for download_id in await _download_ids(session, ("queued", "downloading")):
                    dl = await session.get(VideoDownload, download_id)
                    channel = grab.make_channel(dl.channel, config)
                    try:
                        status = await channel.job_status(dl.job_ref)
                    except AcquireError as exc:
                        # transient worker trouble must not fail the job; keep
                        # polling and surface the reason in the queue UI
                        dl.detail = str(exc)
                        await session.commit()
                        continue
                    lost = acquire.unaccounted_for(status, dl.created_at)
                    dl.progress = status.progress
                    dl.detail = (f"{dl.channel} lost the job: {status.detail}" if lost
                                 else status.detail)
                    if status.state == "failed" or lost:
                        dl.state = "failed"
                        await grab.bury(session, dl)
                        await session.commit()
                        await grab.try_next_release(session, config, dl)
                    elif status.state == "complete":
                        # the folder goes on the row before the import starts:
                        # it is the only way back to the files afterwards, and
                        # the client forgets the job as soon as it is cleaned up
                        dl.state = "downloaded"
                        dl.job_ref = {**dl.job_ref, "landing": status.directory or ""}
                        await session.commit()
                        await _run_import(session, config, dl)
                    else:
                        if status.state == "downloading":
                            dl.state = "downloading"
                        await session.commit()
                await importer.retry_waiting_subtitles(session, config)
                _channel_poll_counter += 1
                if _channel_poll_counter >= WEB_CHANNEL_POLL_EVERY:
                    _channel_poll_counter = 0
                    await _refresh_web_channels(session, config)
        except Exception:
            log.exception("download poll loop iteration failed")
        await asyncio.sleep(settings.poll_interval_seconds)
