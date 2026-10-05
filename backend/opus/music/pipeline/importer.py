"""Tagging and filing a finished download into the library, then cleaning up
after it."""

import asyncio
import logging
import shutil
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from opus import importing
from opus.music.channels import enabled_channels
from opus.db import SessionLocal
from opus.music.metadata import artwork
from opus.models import MusicDownload, MusicDownloadStatus, MusicFile, Release, ReleaseStatus, Track
from opus.music.tagging import tagger
from opus.music.pipeline.arrival import Arrival, TrackRef, begin_import
from opus.music.pipeline import grab, judge, state

log = logging.getLogger("opus.music.pipeline")


async def import_download(download_id: int):
    if download_id in state.importing:
        return
    state.importing.add(download_id)
    try:
        await _import_locked(download_id)
    except Exception as exc:
        # only ImportError_ is handled inside; anything else (a corrupt file
        # mutagen chokes on, an OSError, a bug) would leave the row IMPORTING,
        # which the stranded-retry then repeats forever
        log.exception("import of download %s failed", download_id)
        await grab.fail(download_id, f"import failed: {exc}")
    finally:
        state.importing.discard(download_id)


async def _import_locked(download_id: int):
    arrival = await begin_import(download_id)
    if arrival is None:
        return
    cover_bytes, cover_mime, artist_image_url = await _artwork_for(arrival)

    try:
        plan = await asyncio.to_thread(tagger.plan_import, arrival.source_dir,
                                       arrival.track_refs)
    except tagger.ImportError_ as exc:
        await grab.fail(download_id, str(exc))
        log.error("import failed for download %s: %s", download_id, exc)
        return

    judged, adopted_edition = await judge.judge_import(arrival, plan)
    if not judged:
        return
    filed = await _file_import(arrival, plan, adopted_edition, cover_bytes, cover_mime)
    if filed is None:
        return
    plan, imported, remaining = filed
    await _after_import(arrival, plan, imported, remaining, artist_image_url)


async def _artwork_for(arrival: Arrival) -> tuple[bytes | None, str, str | None]:
    """The cover to embed and the portrait to leave beside the artist's folder."""
    # refresh candidates so the embedded cover is the best available; a failed
    # refresh must not fail the import — fall back to what the catalog has
    try:
        await artwork.refresh_release_artwork(arrival.release_id)
    except Exception:
        log.exception("artwork refresh failed for release %s", arrival.release_id)

    async with SessionLocal() as session:
        cover_url = (await artwork.chosen_url(session, "release", arrival.release_id)
                     or arrival.cover_fallback)
        artist_image_url = (await artwork.chosen_url(session, "artist", arrival.artist_id)
                            or arrival.artist_image_fallback)

    cover_bytes, cover_mime = None, "image/jpeg"
    if cover_url:
        try:
            cover_bytes, cover_mime = await artwork.fetch_image(cover_url)
        except Exception:
            log.exception("cover download failed for release %s", arrival.release_id)
    return cover_bytes, cover_mime, artist_image_url


async def _file_import(arrival: Arrival, plan, adopted_edition, cover_bytes: bytes | None,
                       cover_mime: str):
    """Move the files into the library and write down what arrived. None when the
    download stopped wanting this import or the move failed."""
    download_id = arrival.download_id
    async with SessionLocal() as session, importing.managed(session, Path(arrival.music_dir)) as files:
        await files.hold(f"opus:music-import:{arrival.release_id}")
        download = await session.get(MusicDownload, download_id)
        if download is None or download.status is not MusicDownloadStatus.IMPORTING:
            # cancelled or deleted while the edition checks were running; the
            # import must not write its own verdict over that decision
            log.info("download %s no longer wants this import", download_id)
            return None
        release = await session.get(Release, download.release_id)
        if adopted_edition is not None:
            plan = await _adopt_edition(session, release, arrival, plan, adopted_edition)
        # An album is one edition from one source — but a record can exist in
        # more than one edition, and a surround mix is not a better copy of the
        # stereo one. Which arrived decides where it is written, so the two do
        # not render to a single path and overwrite each other.
        first = next(iter(plan.pairs.values()), None)
        arrived = (
            await asyncio.to_thread(tagger.probe_file, plan.audio_files[first])
            if first is not None else {}
        )
        edition = tagger.edition_of(arrived.get("channels"), arrived.get("codec"))
        imported = await importing.run(
            tagger.prepare_import,
            plan, files, arrival.artist_name, arrival.album_title, arrival.release_date,
            arrival.track_refs, arrival.music_dir, arrival.config.get("music_naming"),
            cover_bytes, cover_mime, edition,
        )
        download = (await session.execute(select(MusicDownload).where(
            MusicDownload.id == download_id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if download is None or download.status is not MusicDownloadStatus.IMPORTING:
            return None
        await files._lock()
        await _record_files(session, files, download_id, release.id, imported)
        download.status = MusicDownloadStatus.COMPLETE
        # complete only when no track is left without a file (a missing-only
        # grab may have filled just part of the gap)
        remaining = (await session.execute(
            select(func.count()).select_from(Track)
            .outerjoin(MusicFile, MusicFile.track_id == Track.id)
            .where(Track.release_id == release.id, MusicFile.id.is_(None))
        )).scalar()
        release.status = (ReleaseStatus.COMPLETE if remaining == 0
                          else ReleaseStatus.NONE)
        await importing.commit(session)
    return plan, imported, remaining


async def _adopt_edition(session, release: Release, arrival: Arrival, plan, adopted_edition):
    """The download IS a complete edition of this album, just not the one the
    catalog lists — the edition on disk becomes the record."""
    track_refs = arrival.track_refs
    listed = len(track_refs)
    existing = await release.awaitable_attrs.tracks
    if adopted_edition.titles:
        # a different pressing: its tracklist replaces the catalog's,
        # which is the only way the extra tracks get rows at all
        for track in existing:
            await session.delete(track)
        await session.flush()
        new_tracks = [
            Track(release_id=release.id, position=index + 1, title=title)
            for index, title in enumerate(adopted_edition.titles)
        ]
        session.add_all(new_tracks)
        await session.flush()
        track_refs = [TrackRef(t.id, t.position, t.title) for t in new_tracks]
        plan = tagger.plan_for(plan, track_refs)
    else:
        # proof by count only: the files are a complete SUBSET of what
        # the catalog lists, so the rows they did not claim go
        for track in existing:
            if track.id not in plan.pairs:
                await session.delete(track)
        track_refs = [t for t in track_refs if t.id in plan.pairs]
    arrival.track_refs = track_refs
    release.track_count = len(track_refs)
    await session.flush()
    log.info("release %s: adopted the downloaded edition via %s "
             "(%d tracks, catalog listed %d)", arrival.release_id,
             adopted_edition.evidence, len(track_refs), listed)
    return plan


async def _record_files(session, files, download_id: int, release_id: int,
                        imported: dict[int, str]) -> None:
    layouts: dict[int, str] = {}
    for track_id, path in imported.items():
        info = await asyncio.to_thread(tagger.probe_file, Path(files.entries[path]["new"]))
        layouts[track_id] = tagger.edition_of(info.get("channels"), info.get("codec"))
        await session.execute(
            pg_insert(MusicFile)
            .values(path=path, track_id=track_id,
                    download_id=download_id, **info)
            .on_conflict_do_update(
                index_elements=["path"],
                set_={**info, "track_id": track_id, "download_id": download_id},
            )
        )
    # A track that just received a fresh file supersedes its old one — a
    # lossy-to-FLAC replacement must not leave both on disk. Only within its
    # own edition, though: a five-channel mix is not a better copy of the
    # stereo master, and a DSD rip is not a better copy of the FLAC one — each
    # is a different record of the same music, and stands beside the other
    # rather than over it.
    candidates = (await session.execute(
        select(MusicFile).where(
            MusicFile.track_id.in_(list(imported.keys())),
            MusicFile.path.notin_(list(imported.values())),
        )
    )).scalars().all()
    superseded = [
        old for old in candidates
        if tagger.edition_of(old.channels, old.codec) == layouts.get(old.track_id, "")
    ]
    for old in superseded:
        files.retire(Path(old.path))
        await session.delete(old)
    if superseded:
        log.info("removed %d superseded files for release %s",
                 len(superseded), release_id)


async def _after_import(arrival: Arrival, plan, imported: dict[int, str], remaining: int,
                        artist_image_url: str | None) -> None:
    download_id, release_id = arrival.download_id, arrival.release_id
    if artist_image_url:
        try:
            image_bytes, image_mime = await artwork.fetch_image(artist_image_url)
            first = Path(next(iter(imported.values())))
            artist_dir = Path(arrival.music_dir) / first.relative_to(arrival.music_dir).parts[0]
            await asyncio.to_thread(tagger.write_folder_image, artist_dir, image_bytes, image_mime)
        except Exception:
            log.exception("artist folder image failed for artist %s", arrival.artist_id)

    if arrival.config.bool("cleanup_after_import"):
        await _cleanup_downloader(download_id, arrival.channel_name, arrival.job_ref,
                                  arrival.source_dir, arrival.config)
    state.live.pop(download_id, None)
    # a box set delivers this album and leaves its other albums where they are:
    # unclaimed files are the post's business, not a shortfall of the import
    log.info("download %s imported (%d tracks%s)", download_id, len(imported),
             f", {len(plan.unclaimed)} files left unclaimed" if plan.unclaimed else "")

    # a whole-album NZB can land a track short; pull the gap from a different NZB
    # (or another channel). grab_release caps attempts and skips already-tried NZBs
    if remaining and remaining > 0:
        log.info("release %s still missing %d track(s) — looking for one "
                 "source that carries the whole album", release_id, remaining)
        # never patch the gap from a second source: a library album should be
        # one edition from one rip. The recovery hunts a complete replacement
        # and is discarded unless it can deliver every track.
        grab.spawn_grab(release_id, mode="replace")


async def _cleanup_downloader(download_id: int, channel_name: str, job_ref: dict,
                              source_dir: Path, config):
    try:
        root = Path(config.get("opus_landing_dir")).resolve()
        resolved = source_dir.resolve()
        # the guard stays whatever reports the path: the landing root holds every
        # other job's files, so it is never the thing being removed
        if resolved != root and root in resolved.parents:
            await asyncio.to_thread(shutil.rmtree, resolved)
    except Exception:
        log.exception("landing zone cleanup failed for download %s", download_id)

    channels = {c.name: c for c in enabled_channels(config)}
    channel = channels.get(channel_name)
    if channel is None:
        log.error("cleanup for download %s: channel %s is not enabled, job left in client",
                  download_id, channel_name)
        return
    try:
        await channel.drop_job(job_ref)
    except Exception:
        log.exception("download client job removal failed for download %s", download_id)
