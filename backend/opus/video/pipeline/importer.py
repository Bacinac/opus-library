"""The acceptance test: a finished download becomes a library file only when
it probes sound and its subtitle policy is satisfied."""

import asyncio
import datetime
import logging
import shutil
from pathlib import Path
from guessit import guessit

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from opus.acquire import AcquireError
from opus.config import settings
from opus.models import VideoDownload, VideoFile, Movie, Subtitle, WebVideo
from opus.settings_store import RuntimeConfig
from opus.video.subtitles import extract, fetcher, opensubtitles
from opus.video.subtitles.policy import effective_policy, evaluate
from opus.video.subtitles.probe import (VIDEO_EXTENSIONS, find_video_files, own_sidecars,
                                        probe_file, sidecar_subs, tail_ok)
from opus.video.pipeline import items, naming, profiles

log = logging.getLogger("opus.video.pipeline")


class ImportFailure(Exception):
    """A completed download that could not be turned into a library file."""


def _completed_dir(config: RuntimeConfig, dl: VideoDownload, directory: str) -> Path:
    """Where the job's files are, as the library sees them.

    One swap and no searching. OPUS reports the exact folder in its own view of
    the landing tree — it reads each engine's real download root from that
    engine and refuses a path outside it — and both apps bind the same host
    directory, so the only thing left is to exchange one mount point for the
    other. Guessing which of two roots and whether a category folder had been
    inserted was a guess only because each client answered in its own terms."""
    root = Path(config.get("opus_landing_root"))
    try:
        relative = Path(directory).relative_to(root)
    except ValueError as exc:
        raise AcquireError(
            f"OPUS reported {directory!r}, which is not inside its landing root "
            f"{root} — the two mounts no longer agree"
        ) from exc
    return Path(config.get("opus_landing_dir")) / relative


def _move_into_place(source: Path, dest: Path) -> None:
    """Copy to a neighbour, then swap it in.

    A move across a mount is a copy, and a copy that is interrupted leaves a
    torn file sitting at the library path under the name of a whole one —
    indistinguishable from the real thing until somebody plays the last ten
    minutes. The rename at the end is the only step anything else can observe,
    and a rename cannot be observed half done."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_name(dest.name + ".part")
    staged.unlink(missing_ok=True)
    try:
        shutil.move(str(source), str(staged))
        staged.replace(dest)
    finally:
        staged.unlink(missing_ok=True)


def _sidecar_names(dest: Path, sidecars: list[dict]) -> list[Path]:
    """Where each sidecar goes beside its video: `<stem>.<lang>.<format>`, with a
    number before the language for a second one of the same language, so no
    sidecar is moved over another. One already named for this video stays."""
    sources = {Path(sub["path"]) for sub in sidecars}
    placed = {path for path in sources
              if path.parent == dest.parent and path.name.startswith(f"{dest.stem}.")}
    names = []
    for sub in sidecars:
        name = Path(sub["path"])
        if name not in placed:
            n = 1
            name = dest.with_name(f"{dest.stem}.{sub['lang']}.{sub['format']}")
            while name in sources or name in placed:
                n += 1
                name = dest.with_name(f"{dest.stem}.{n}.{sub['lang']}.{sub['format']}")
            placed.add(name)
        names.append(name)
    return names


async def _persist_file(session, dl: VideoDownload, config: RuntimeConfig,
                        source: Path, dest: Path) -> VideoFile:
    """Probe, move into the library (with subtitle sidecars) and persist the
    files-first rows. Returns the VideoFile with subtitles loaded."""
    if source == dest:
        sidecars = own_sidecars(dest)
    else:
        sidecars = sidecar_subs(source)
        # off the event loop: this is a five-gigabyte copy, and running it here
        # stops the API, the poll loops and every other request for its whole
        # duration — four minutes of the module answering nothing
        await asyncio.to_thread(_move_into_place, source, dest)
    info = await probe_file(dest)

    # A path already on record means this exact file was fetched before — a
    # re-download of something the library already holds. The file on disk is
    # the new one, so the row describes the new one; inserting a second row for
    # the same path fails on the unique index and strands the download at
    # `downloaded` forever.
    existing = await session.execute(select(VideoFile).where(VideoFile.path == str(dest)))
    media = existing.scalar_one_or_none()
    superseded: set[str] = set()
    if media is None:
        media = VideoFile(path=str(dest))
        session.add(media)
    else:
        superseded = {p for sub in await _subtitles_for_file(session, media.id)
                      for p in (sub.path, sub.vtt_path) if p}
        await session.execute(delete(Subtitle).where(Subtitle.file_id == media.id))
    media.size = dest.stat().st_size
    media.container = info["container"]
    media.video_codec = info["video_codec"]
    media.width = info["width"]
    media.height = info["height"]
    media.audio_langs = info["audio_langs"]
    media.duration_s = info.get("duration_s")
    media.release_guid = dl.release_guid
    media.release_title = dl.release_title
    media.movie_id = dl.movie_id
    media.episode_id = dl.episode_id
    media.web_video_id = dl.web_video_id
    await session.flush()

    await profiles.persist_streams(session, media, info)

    # read the wanted embedded tracks out now, while the file is being imported
    # anyway — asking for one at the moment somebody presses a button means a
    # three-minute read of the container, and nothing waits that long
    wanted = [
        {"position": i, "lang": s["lang"]}
        for i, s in enumerate(info["subtitle_streams"])
        if (s["codec"] or "").lower() in extract.TEXT_CODECS
        and (not config.langs() or s["lang"] in config.langs())
    ]
    written = await extract.extract(dest, wanted) if wanted else {}
    for i, stream in enumerate(info["subtitle_streams"]):
        session.add(Subtitle(file_id=media.id, lang=stream["lang"], source="embedded",
                             format=stream["codec"], forced=stream["forced"],
                             stream_index=i, vtt_path=written.get(i),
                             hollow=i in written and written[i] is None))
    for sub, sub_dest in zip(sidecars, _sidecar_names(dest, sidecars)):
        sub_source = Path(sub["path"])
        if sub_source != sub_dest:
            shutil.move(str(sub_source), str(sub_dest))
        auto = ".auto." in sub_source.name.lower()
        session.add(Subtitle(file_id=media.id, lang=sub["lang"], source="external",
                             format=sub["format"], forced=sub["forced"], auto_generated=auto,
                             path=str(sub_dest)))
    await session.flush()
    # the previous copy's subtitles were read out of a different container
    # under numbered names, and nothing refers to them once their rows are gone
    for stale in superseded - {p for sub in await _subtitles_for_file(session, media.id)
                               for p in (sub.path, sub.vtt_path) if p}:
        await asyncio.to_thread(Path(stale).unlink, missing_ok=True)
    return media


async def _subtitles_for_file(session, file_id: int) -> list[Subtitle]:
    result = await session.execute(select(Subtitle).where(Subtitle.file_id == file_id))
    return list(result.scalars())


async def _adopted_file(session, config: RuntimeConfig, dl: VideoDownload) -> Path | None:
    """The library file a half-finished import already left behind, if it holds.

    Between the rename and the commit the file is in the library and nothing
    says so; a run that ends there has done all the expensive work and only
    owes the row. What it left is read to the end before it is believed,
    because a copy from before the rename was atomic can be torn — and a torn
    one is removed rather than catalogued, which puts the item back in front of
    the monitor instead of into the shelf."""
    if dl.kind == "episode":
        column, item_id = VideoFile.episode_id, dl.episode_id
        base = naming.render_episode_path(config, await items.episode_with_series(session, dl.episode_id), "")
    elif dl.kind == "movie":
        column, item_id = VideoFile.movie_id, dl.movie_id
        base = naming.render_movie_path(config, await session.get(Movie, dl.movie_id), "")
    else:
        return None

    # only an item the library cannot account for is one this import owes a row
    # for. If a file is already on record, the empty landing folder means this
    # particular grab produced nothing, and answering with the copy that is
    # already shelved would report a fetch that never happened.
    known = await session.execute(select(VideoFile.id).where(column == item_id).limit(1))
    if known.scalar_one_or_none() is not None:
        return None
    for suffix in sorted(VIDEO_EXTENSIONS):
        candidate = base.with_name(base.name + suffix)
        if not candidate.exists():
            continue
        if await tail_ok(candidate):
            log.info("download %s: adopting the file an earlier import left at %s",
                     dl.id, candidate)
            return candidate
        log.warning("download %s: %s was left half-copied; removing it", dl.id, candidate)
        candidate.unlink()
    return None


async def import_download(session, config: RuntimeConfig, dl: VideoDownload,
                           directory: str) -> None:
    source_dir = _completed_dir(config, dl, directory) if directory else None
    videos = find_video_files(source_dir) if source_dir and source_dir.exists() else []

    if not videos:
        # the files are not where the client left them. Either they never
        # arrived, or an earlier run moved them into the library and did not
        # live to say so — from here the two look the same, and only one of
        # them is recoverable.
        adopted = await _adopted_file(session, config, dl)
        if adopted is None:
            raise ImportFailure(
                f"completed download not found at {source_dir}" if source_dir
                else "the completed download folder was never recorded")
        source = dest = adopted
    else:
        source = videos[0]
        if dl.kind == "episode":
            episode = await items.episode_with_series(session, dl.episode_id)
            for candidate in videos:
                parsed = guessit(candidate.name)
                if parsed.get("episode") == episode.number:
                    source = candidate
                    break
            dest = naming.render_episode_path(config, episode, source.suffix)
        elif dl.kind == "movie":
            movie = await session.get(Movie, dl.movie_id)
            dest = naming.render_movie_path(config, movie, source.suffix)
        else:
            video = await session.get(WebVideo, dl.web_video_id)
            dest = naming.render_web_path(config, video, naming.safe(source.name))

    media = await _persist_file(session, dl, config, source, dest)
    if dl.kind in ("episode", "movie"):
        await _retire_replaced(session, media)
    await _finalize_policy(session, config, dl, media)


async def _retire_replaced(session, media: VideoFile) -> None:
    """A download that lands on an item already on the shelf is its replacement:
    the monitor never fetches what is held, so only somebody asking for a better
    copy brings one. The old file goes once the new one is in place, sidecars and
    extracted subtitles with it — two copies of one episode are a shelf that
    answers with whichever it lists first."""
    column = VideoFile.episode_id if media.episode_id else VideoFile.movie_id
    item = media.episode_id or media.movie_id
    old = (await session.execute(
        select(VideoFile).where(column == item, VideoFile.id != media.id)
        .options(selectinload(VideoFile.subtitles)))).scalars().all()
    # a sidecar keeps the video's stem, so an .mkv replaced by an .mp4 has put
    # its own subtitles exactly where the old ones were named
    kept = {media.path} | {p for sub in await _subtitles_for_file(session, media.id)
                           for p in (sub.path, sub.vtt_path) if p}
    for file in old:
        paths = [file.path] + [p for sub in file.subtitles for p in (sub.path, sub.vtt_path) if p]
        for path in paths:
            if path not in kept:
                await asyncio.to_thread(Path(path).unlink, missing_ok=True)
        log.info("replaced %s with %s", file.path, media.path)
        await session.delete(file)
    await session.flush()


async def _finalize_policy(session, config: RuntimeConfig, dl: VideoDownload,
                           media: VideoFile) -> None:
    override = ""
    if dl.movie_id:
        override = (await session.get(Movie, dl.movie_id)).subtitle_override
    elif dl.episode_id:
        episode = await items.episode_with_series(session, dl.episode_id)
        override = episode.season.series.subtitle_override
    policy = effective_policy(config, override)
    accept_auto = config.bool("accept_auto_subs")

    subs = await _subtitles_for_file(session, media.id)
    result = evaluate(policy, subs, accept_auto=accept_auto)
    if not result.satisfied:
        try:
            await fetcher.acquire_missing(session, config, media, result.missing,
                                          query_hint=dl.release_title)
        except opensubtitles.QuotaExhausted as exc:
            log.warning("subtitles for %s must wait: %s", media.path, exc)
        subs = await _subtitles_for_file(session, media.id)
        result = evaluate(policy, subs, accept_auto=accept_auto)

    dl.job_ref = {**dl.job_ref, "file_id": media.id}
    if result.satisfied:
        dl.state = "imported"
        dl.progress = 1.0
        dl.detail = ""
    else:
        dl.state = "waiting_subtitles"
        dl.progress = 1.0
        dl.detail = ",".join(result.missing)
        # the retry clock: an unchanged row emits no UPDATE, so onupdate never moves it
        dl.updated_at = datetime.datetime.now(datetime.UTC)
        log.info("download %s imported but waiting for subtitles: %s", dl.id, dl.detail)


async def retry_waiting_subtitles(session, config: RuntimeConfig) -> None:
    cutoff = (datetime.datetime.now(datetime.UTC)
              - datetime.timedelta(minutes=settings.subtitle_retry_minutes))
    result = await session.execute(
        select(VideoDownload).where(VideoDownload.state == "waiting_subtitles",
                               VideoDownload.updated_at < cutoff))
    for dl in result.scalars():
        file_id = dl.job_ref.get("file_id")
        media = await session.get(VideoFile, file_id) if file_id else None
        if media is None:
            dl.state = "failed"
            dl.detail = "imported file row is gone"
            continue
        await _finalize_policy(session, config, dl, media)
    await session.commit()
