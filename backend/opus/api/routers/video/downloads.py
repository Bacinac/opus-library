"""A video download the user is done with. The listing is not here — both halves
of the library answer one queue, and that queue lives at /api/downloads."""

import logging

from fastapi import APIRouter, Depends

from opus.acquire import AcquireError
from opus.db import get_session
from opus.models import VideoDownload
from opus.settings_store import current_runtime
from opus.video.pipeline import grab, items

log = logging.getLogger(__name__)
router = APIRouter()


@router.delete("/downloads/{download_id}", status_code=204)
async def download_delete(download_id: int, session=Depends(get_session)):
    config = await current_runtime()
    dl = await session.get(VideoDownload, download_id)
    if dl is None:
        return
    if dl.state in items.ACTIVE_STATES:
        try:
            await grab.make_channel(dl.channel, config).drop_job(dl.job_ref)
        except AcquireError as exc:
            log.warning("could not remove job for download %s: %s", download_id, exc)
    await session.delete(dl)
    # the request session is not committed for us: without this the job is taken
    # off the client, 204 is answered, and the row is still there afterwards
    await session.commit()
