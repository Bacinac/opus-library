"""Starting a download for a release, and settling one that ends without an
import."""

import asyncio
import logging

from sqlalchemy import func, select, update

from opus import passes
from opus.music.channels import enabled_channels
from opus.acquire import AcquireError
from opus.music.channels.base import Candidate, Channel
from opus.db import SessionLocal
from opus.music.matching import quality
from opus.music.matching.engine import ScoredCandidate
from opus.models import MusicDownload, MusicDownloadStatus, MusicFile, Release, ReleaseStatus, Track
from opus.settings_store import current_runtime
from opus.music.pipeline import choose, state

log = logging.getLogger("opus.music.pipeline")


# an incomplete whole-album grab auto-recovers (another NZB, then another channel);
# cap total downloads per release so a gap nothing can fill never loops forever
MAX_RECOVERY_DOWNLOADS = 6


def spawn_grab(release_id: int, mode: str = "full"):
    passes.spawn(grab_release(release_id, mode))


# a mass redownload must not fire dozens of simultaneous searches at
# slskd/Prowlarr — two grabs in flight, the rest queue behind the semaphore
_bulk_sem = asyncio.Semaphore(2)


def spawn_grab_bulk(release_ids: list[int], mode: str = "full"):
    async def _one(release_id: int):
        async with _bulk_sem:
            await grab_release(release_id, mode)

    for release_id in release_ids:
        passes.spawn(_one(release_id))


async def grab_candidate(release_id: int, channel_name: str, title: str, ref: dict,
                         mode: str = "full", multi_album: bool = False) -> bool:
    """The human's own pick, downloaded exactly as chosen — no automatic
    matching, no manifest re-check, no title-completeness gate. What the
    picker above surfaces and the automatic search would have refused reaches
    the library this way instead. Import-time judgement is the only gate
    left standing: a packed or obfuscated post fails there exactly as an
    automatic one would."""
    config = await current_runtime()
    channel = next((c for c in enabled_channels(config) if c.name == channel_name), None)
    if channel is None:
        log.error("release %s: %r is not an enabled channel", release_id, channel_name)
        return False
    candidate = Candidate(channel=channel_name, title=title, files=[], ref=ref,
                          whole_album=True)
    try:
        job_ref = await channel.download(candidate, None)
    except AcquireError as exc:
        log.error("release %s: %s grab of %r failed: %s",
                 release_id, channel_name, title, exc)
        return False
    # scored 100/whole: a human already judged this post fit to grab, and
    # there is no automatic reading of it to record beside that judgement
    best = ScoredCandidate(candidate, 100.0, 1.0, 100.0, quality.Quality(), multi_album)
    return await _record_download(release_id, channel_name, best, mode, job_ref)


async def grab_release(release_id: int, mode: str = "full"):
    """An album is one edition from one source at a time.

    full: acquire it. replace: the same search, but the download is discarded at
    import unless it delivers every track — the recovery for an album that came
    up short. surround and dsd: the OTHER edition of a record already held,
    which is not a better copy of it and does not replace it; each is written
    into a folder of its own inside the album's and stands beside the stereo
    one — dsd refuses anything wider than stereo, since nothing in the house
    plays a multichannel DSD stream.

    There is deliberately no way to fetch only the missing tracks of an edition:
    two rips of one album differ in mastering and level even at identical specs,
    and stitching them is what made albums sound uneven."""
    try:
        await _grab_release(release_id, mode)
    except Exception:
        log.exception("grab of release %s failed", release_id)
        await settle_interrupted(release_id)


async def settle_interrupted(release_id: int | None = None) -> None:
    held = (select(func.count()).select_from(MusicFile)
            .join(Track, Track.id == MusicFile.track_id)
            .where(Track.release_id == Release.id).scalar_subquery())
    stuck = Release.status.in_((ReleaseStatus.WANTED, ReleaseStatus.SEARCHING))
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Release.id, held).where(
                stuck, *([Release.id == release_id] if release_id is not None else []))
        )).all()
        for row_id, count in rows:
            await session.execute(
                update(Release).where(Release.id == row_id, stuck)
                .values(status=ReleaseStatus.NONE if count else ReleaseStatus.FAILED))
        await session.commit()
    if rows and release_id is None:
        log.warning("releases left mid-search by the last run settled: %s",
                    ", ".join(str(row_id) for row_id, _ in rows))


async def _grab_release(release_id: int, mode: str):
    automatic = mode == "replace"
    brief = await choose.brief(release_id, automatic)
    if brief is None:
        return
    if automatic and brief.download_count >= MAX_RECOVERY_DOWNLOADS:
        await _leave_partial(release_id)
        return
    for channel in choose.channels_to_ask(brief.config, automatic):
        if channel.name in brief.failed_channels:
            continue
        best = await choose.best_on_channel(release_id, channel, brief, mode)
        if best is None:
            continue
        # a search runs for minutes, and a bulk delete or the orphan sweep
        # can take the release out from under it — the verdict then has
        # nowhere to land, and the task must not die on the way out
        if not await _still_there(release_id):
            log.info("release %s: deleted while it was being searched", release_id)
            return
        if await _start_download(release_id, channel, best, mode):
            return
    await _settle_unfound(release_id, automatic)


async def _still_there(release_id: int) -> bool:
    async with SessionLocal() as session:
        return await session.get(Release, release_id) is not None


async def _set_status(release_id: int, status: ReleaseStatus) -> bool:
    async with SessionLocal() as session:
        release = await session.get(Release, release_id, with_for_update=True)
        if release is None:
            return False
        release.status = status
        await session.commit()
    return True


async def _leave_partial(release_id: int) -> None:
    if not await _set_status(release_id, ReleaseStatus.NONE):
        log.info("release %s: deleted while it was being searched", release_id)
        return
    log.info("release %s: recovery cap (%d) reached, leaving album partial",
             release_id, MAX_RECOVERY_DOWNLOADS)


async def _start_download(release_id: int, channel: Channel, best: ScoredCandidate,
                          mode: str) -> bool:
    try:
        job_ref = await channel.download(best.candidate, None)
    except AcquireError as exc:
        log.error("channel %s download failed: %s", channel.name, exc)
        return False
    if await _record_download(release_id, channel.name, best, mode, job_ref):
        log.info("release %s: grabbed via %s (score %.1f)", release_id, channel.name, best.score)
    else:
        await _drop_orphan(release_id, channel, job_ref)
    return True


async def _record_download(release_id: int, channel_name: str, best: ScoredCandidate,
                           mode: str, job_ref: dict) -> bool:
    async with SessionLocal() as session:
        release = await session.get(Release, release_id, with_for_update=True)
        if release is None:
            return False
        release.status = ReleaseStatus.DOWNLOADING
        session.add(MusicDownload(
            release_id=release_id, channel=channel_name,
            status=MusicDownloadStatus.DOWNLOADING, score=best.score,
            # the import needs to know this was a gap-fill: a whole-album
            # channel answers one anyway (an NZB is all-or-nothing), and
            # importing every track would overwrite the files already held.
            # multi_album says the post brings other albums down with it,
            # so the import must not read its extra files as this album's
            job_ref={**job_ref, "mode": mode,
                     "multi_album": best.multi_album},
        ))
        await session.commit()
    return True


async def _drop_orphan(release_id: int, channel: Channel, job_ref: dict) -> None:
    try:
        await channel.drop_job(job_ref)
    except AcquireError as exc:
        log.error("release %s: deleted as its download started, and job %s could not "
                  "be dropped: %s", release_id, job_ref.get("job"), exc)
        return
    log.info("release %s: deleted as its download started, job %s dropped",
             release_id, job_ref.get("job"))


async def _settle_unfound(release_id: int, automatic: bool) -> None:
    # recovery keeps whatever was already imported; only a from-scratch grab
    # with nothing anywhere is a hard failure
    status = ReleaseStatus.NONE if automatic else ReleaseStatus.FAILED
    if not await _set_status(release_id, status):
        log.info("release %s: deleted while it was being searched", release_id)
        return
    if automatic:
        log.info("release %s: no source carries the whole album, left as it was",
                 release_id)
    else:
        log.error("release %s: no viable candidate on any channel", release_id)


async def reject(download_id: int, detail: str):
    """The download was sound and the policy turned it away. Same recovery as a
    failure — hunt another source — but the row must not read as broken."""
    await _settle(download_id, MusicDownloadStatus.REJECTED, detail)


async def fail(download_id: int, detail: str):
    await _settle(download_id, MusicDownloadStatus.FAILED, detail)


async def _settle(download_id: int, status: MusicDownloadStatus, detail: str):
    async with SessionLocal() as session:
        download = await session.get(MusicDownload, download_id)
        if download is None:
            # deleted from the history while it was still being worked on
            log.info("download %s is gone, nothing to settle", download_id)
            return
        download.status = status
        download.error = detail
        release_id = download.release_id
        release = await session.get(Release, download.release_id)
        held = (await session.execute(
            select(func.count()).select_from(MusicFile)
            .join(Track, Track.id == MusicFile.track_id)
            .where(Track.release_id == release_id)
        )).scalar()
        # FAILED means the album has nothing; one that still holds most of its
        # tracks is simply not complete, and must stay out of the bulk actions
        # that treat FAILED as "there is nothing here to lose"
        release.status = ReleaseStatus.FAILED if not held else ReleaseStatus.NONE
        await session.commit()
    state.live.pop(download_id, None)
    log.log(logging.ERROR if status is MusicDownloadStatus.FAILED else logging.INFO,
            "download %s %s: %s", download_id, status.value, detail)
    # try the next source — grab_release skips channels already failed for
    # this release, and settles on FAILED only once every channel is exhausted.
    # An album that already holds files wants a REPLACEMENT, never a patch
    # from a second source.
    spawn_grab(release_id, mode="replace" if held else "full")
