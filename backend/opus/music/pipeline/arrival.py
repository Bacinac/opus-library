"""What a finished download brought, gathered once before anything is judged
or filed."""

import logging
from dataclasses import dataclass
from pathlib import Path

from opus.db import SessionLocal
from opus.models import MusicDownload, MusicDownloadStatus, Release, ReleaseStatus
from opus.settings_store import current_runtime
from opus.music.tagging import tagger
from opus.music.pipeline import state

log = logging.getLogger("opus.music.pipeline")


@dataclass(frozen=True)
class TrackRef:
    """The catalog track as the importer needs it, detached from the session.
    duration_sec carries the matcher's corroboration signal — without it a
    renamed track cannot be rescued by position and length."""

    id: int
    position: int
    title: str
    duration_sec: int | None = None


def _resolve_source_dir(download: MusicDownload, config) -> Path:
    """Where the job's files are, as the library sees them.

    One translation and no searching. OPUS reports the exact folder in its own
    view of the landing tree — it reads each engine's own download root from that
    engine and refuses a path that falls outside it — and both apps bind the same
    host directory, so the only thing left to do is swap one mount point for the
    other.

    What used to be here took the basename and then walked the whole landing tree
    for any folder of that name, because the download clients reported paths in
    their own namespaces. That search is precisely how a foreign album's folder
    could be imported into this release, and removing the need for it is what
    OPUS was built for."""
    reported = download.job_ref.get("directory", "")
    if not reported:
        raise tagger.ImportError_(f"download {download.id} reports no folder")

    root = Path(config.get("opus_landing_root"))
    try:
        relative = Path(reported).relative_to(root)
    except ValueError:
        raise tagger.ImportError_(
            f"OPUS reported {reported!r}, which is not inside its landing root "
            f"{root} — the two mounts no longer agree"
        )
    local = Path(config.get("opus_landing_dir")) / relative
    if not local.is_dir():
        raise tagger.ImportError_(f"the job's folder is gone from {local}")
    return local


@dataclass
class Arrival:
    """A download about to be imported, as far as the import needs it once the
    first session has closed."""

    download_id: int
    release_id: int
    artist_id: int
    artist_name: str
    album_title: str
    release_date: str | None
    cover_fallback: str | None
    artist_image_fallback: str | None
    mode: str
    multi_album: bool
    track_refs: list[TrackRef]
    source_dir: Path
    music_dir: str
    channel_name: str
    job_ref: dict
    config: object


async def begin_import(download_id: int) -> Arrival | None:
    async with SessionLocal() as session:
        download = await session.get(MusicDownload, download_id)
        if download is None:
            log.info("download %s is gone, nothing to import", download_id)
            return None
        download.status = MusicDownloadStatus.IMPORTING
        await session.commit()

        release = await session.get(Release, download.release_id)
        artist = await release.awaitable_attrs.artist
        tracks = await release.awaitable_attrs.tracks
        config = await current_runtime()
        try:
            source_dir = _resolve_source_dir(download, config)
        except tagger.ImportError_ as exc:
            download.status = MusicDownloadStatus.FAILED
            download.error = str(exc)
            release.status = ReleaseStatus.FAILED
            await session.commit()
            state.live.pop(download_id, None)
            log.error("import failed for download %s: %s", download_id, exc)
            return None
        return Arrival(
            download_id=download_id, release_id=release.id, artist_id=artist.id,
            artist_name=artist.name, album_title=release.title,
            release_date=release.release_date, cover_fallback=release.cover_url,
            artist_image_fallback=artist.image_url,
            mode=download.job_ref.get("mode", "full"),
            multi_album=download.job_ref.get("multi_album", False),
            track_refs=[TrackRef(t.id, t.position, t.title, t.duration_sec) for t in tracks],
            source_dir=source_dir, music_dir=config.get("music_dir"),
            channel_name=download.channel, job_ref=download.job_ref, config=config)
