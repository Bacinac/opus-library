"""Acting on a music download: cancelling one in flight, forgetting one, and
clearing what has finished. The listing is the one queue at /api/downloads."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from opus.music.pipeline import state
from opus.music.channels import enabled_channels
from opus.db import get_session
from opus.models import DOWNLOAD_SETTLED, MusicDownload, MusicDownloadStatus, Release, ReleaseStatus
from opus.settings_store import current_runtime

log = logging.getLogger("opus.api")

router = APIRouter()


@router.post("/downloads/{download_id}/cancel")
async def cancel_download(download_id: int,
                          session: AsyncSession = Depends(get_session)):
    """Stop a transfer that is still in flight and drop it from the download
    client. The release goes back to wanting nothing in particular — cancelling
    is a decision not to have this album from this source right now, so no
    recovery grab is spawned behind it."""
    download = await session.get(MusicDownload, download_id)
    if download is None:
        raise HTTPException(404, "download not found")
    if download.status in DOWNLOAD_SETTLED:
        raise HTTPException(409, f"download is already {download.status}")
    _refuse_while_importing(download)

    config = await current_runtime()
    await _abandon(download, config)

    download.status = MusicDownloadStatus.CANCELLED
    download.error = "cancelled"
    release = await session.get(Release, download.release_id)
    if release.status in (ReleaseStatus.SEARCHING, ReleaseStatus.DOWNLOADING):
        release.status = ReleaseStatus.NONE
    await session.commit()
    state.live.pop(download_id, None)
    state.searching.pop(download.release_id, None)
    log.info("download %s cancelled by the user", download_id)
    return {"status": "cancelled"}


@router.delete("/downloads/{download_id}")
async def delete_download(download_id: int,
                          session: AsyncSession = Depends(get_session)):
    """Remove one row from the history. A download still running is stopped
    first — deleting the record of a transfer that keeps going would leave the
    client working on something nothing points at."""
    download = await session.get(MusicDownload, download_id)
    if download is None:
        raise HTTPException(404, "download not found")
    _refuse_while_importing(download)
    if download.status not in DOWNLOAD_SETTLED:
        config = await current_runtime()
        await _abandon(download, config)
        release = await session.get(Release, download.release_id)
        if release.status in (ReleaseStatus.SEARCHING, ReleaseStatus.DOWNLOADING):
            release.status = ReleaseStatus.NONE
    await session.delete(download)
    await session.commit()
    state.live.pop(download_id, None)
    return {"deleted": 1}


@router.post("/downloads/clear")
async def clear_downloads(session: AsyncSession = Depends(get_session)):
    """Empty the history of everything that has finished, whichever way. Rows
    still in flight are left alone."""
    result = await session.execute(
        delete(MusicDownload)
        .where(MusicDownload.status.in_(DOWNLOAD_SETTLED))
        .returning(MusicDownload.id)
    )
    removed = [row[0] for row in result.all()]
    await session.commit()
    for download_id in removed:
        state.live.pop(download_id, None)
    return {"deleted": len(removed)}


def _refuse_while_importing(download: MusicDownload) -> None:
    """An import owns the row and the files under it: telling the client to
    destroy the job now would pull the folder out from under a move in
    progress, and the import would write its own verdict over the user's
    anyway. It is seconds to tens of seconds — the caller waits."""
    if (download.status is MusicDownloadStatus.IMPORTING
            or download.id in state.importing):
        raise HTTPException(409, "the import is running, try again when it ends")


async def _abandon(download: MusicDownload, config) -> None:
    """Tell the download client to forget the job. A client that refuses is
    logged and not allowed to block the cancellation — the row must not stay
    stuck 'downloading' because SABnzbd was unreachable."""
    channel = next((c for c in enabled_channels(config)
                    if c.name == download.channel), None)
    if channel is None or not download.job_ref:
        return
    try:
        await channel.drop_job(download.job_ref)
    except Exception:
        log.exception("could not remove job for download %s from %s",
                      download.id, download.channel)
