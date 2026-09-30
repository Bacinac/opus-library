"""Starting a download, and taking the next release down the list when one
fails."""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from opus.acquire import AcquireError
from opus.video.channels.base import Candidate, Channel
from opus.video.channels.qbittorrent import QbittorrentChannel
from opus.video.channels.sabnzbd import SabnzbdChannel
from opus.video.channels.ytdlp import YtdlpChannel
from opus.models import DeadPost, VideoDownload, Episode, VideoFile, Movie, WebVideo
from opus.settings_store import RuntimeConfig
from opus.video.pipeline import items, search

log = logging.getLogger("opus.video.pipeline")


WEB_VIDEO_NAMESPACE = "video"


def make_channel(name: str, config: RuntimeConfig) -> Channel:
    return {"sabnzbd": SabnzbdChannel, "qbittorrent": QbittorrentChannel,
            "ytdlp": YtdlpChannel}[name](config)


async def rejected_posts(session, *, episode_id: int | None = None,
                         movie_id: int | None = None) -> set[str]:
    """The posts not to take for this item, by the indexer's own name for each:
    those that failed, the one on its way, and the one the file on the shelf
    came down in. A post missing articles does not fill itself back in; a
    re-post is a different upload under the same title, and a title is no
    reason to refuse it."""
    return set(await standings(session, episode_id=episode_id, movie_id=movie_id))


async def standings(session, *, episode_id: int | None = None,
                    movie_id: int | None = None) -> dict[str, str]:
    """What became of each post already taken for this item: "held", "coming"
    or "failed"."""
    item = episode_id if episode_id is not None else movie_id
    dead = await session.scalars(select(DeadPost.guid))
    coming = await session.scalars(
        select(VideoDownload.release_guid).where(
            (VideoDownload.episode_id if episode_id is not None else VideoDownload.movie_id) == item,
            VideoDownload.state.in_(items.ACTIVE_STATES),
            VideoDownload.release_guid != ""))
    held = await held_posts(session, episode_id=episode_id, movie_id=movie_id)
    return dict.fromkeys(dead, "failed") | dict.fromkeys(coming, "coming") | dict.fromkeys(held, "held")


async def bury(session, dl: VideoDownload) -> None:
    if dl.release_guid:
        await session.execute(
            insert(DeadPost).values(guid=dl.release_guid, release_title=dl.release_title,
                                    detail=dl.detail)
            .on_conflict_do_nothing(index_elements=[DeadPost.guid]))


def standing(candidate: Candidate, said: dict[str, str]) -> str:
    return said.get(candidate.guid) or said.get(candidate.title) or ""


async def held_posts(session, *, episode_id: int | None = None,
                     movie_id: int | None = None) -> set[str]:
    """The posts the files already on the shelf for this item came down in: by
    guid, or by release title for a file fetched before guids were kept."""
    item = episode_id if episode_id is not None else movie_id
    result = await session.execute(
        select(VideoFile.release_guid, VideoFile.release_title).where(
            (VideoFile.episode_id if episode_id is not None else VideoFile.movie_id) == item))
    return {guid or title for guid, title in result if guid or title}


def is_taken(candidate: Candidate, rejected: set[str]) -> bool:
    return bool(candidate.guid and candidate.guid in rejected) or candidate.title in rejected


def next_candidate(candidates: list[Candidate], rejected: set[str]) -> Candidate | None:
    for candidate in candidates:
        if not is_taken(candidate, rejected):
            return candidate
    return None


async def try_next_release(session, config: RuntimeConfig, dl: VideoDownload) -> None:
    """Take the next release down the list, now rather than at the next sweep.

    Only one chain at a time: another download already on its way for this item
    means some other failure is being answered already, and answering both walks
    the list twice — which is how a single episode once spent an afternoon
    taking thirty-two releases in matched pairs.

    The walk is not capped. A post is barred by its own guid, so a title taken
    down across the backbone costs only its dead posts, and a re-post of it is
    a new guid, free to be taken the moment it is indexed.
    """
    if dl.kind == "episode":
        item = {"episode_id": dl.episode_id}
    elif dl.kind == "movie":
        item = {"movie_id": dl.movie_id}
    else:
        return
    if await items.coming(session, **item):
        return

    movie = episode = None
    try:
        if dl.kind == "episode":
            episode = await items.episode_with_series(session, dl.episode_id)
            candidates = await search.search_candidates(config, episode=episode, session=session)
            rejected = await rejected_posts(session, episode_id=dl.episode_id)
        else:
            movie = await session.get(Movie, dl.movie_id)
            candidates = await search.search_candidates(config, movie=movie, session=session)
            rejected = await rejected_posts(session, movie_id=dl.movie_id)
        choice = next_candidate(candidates, rejected)
        if choice is None:
            log.warning("download %s failed and every release for %r has now been tried",
                        dl.id, dl.release_title)
            return
        await grab(session, config, choice, movie=movie, episode=episode)
        log.info("download %s failed on %r; grabbed %r instead",
                 dl.id, dl.release_title, choice.title)
    except AcquireError as exc:
        log.warning("could not replace failed download %s: %s", dl.id, exc)


async def grab(session, config: RuntimeConfig, candidate: Candidate, *,
               movie: Movie | None = None, episode: Episode | None = None) -> VideoDownload:
    category = config.get("category_movies") if movie is not None else config.get("category_tv")
    channel = make_channel(candidate.channel, config)
    job_ref = await channel.download(candidate, category)
    dl = VideoDownload(
        kind="movie" if movie is not None else "episode",
        movie_id=movie.id if movie is not None else None,
        episode_id=episode.id if episode is not None else None,
        channel=candidate.channel, release_title=candidate.title, release_guid=candidate.guid,
        job_ref=job_ref, state="queued",
    )
    session.add(dl)
    await session.commit()
    return dl


async def grab_web_video(session, config: RuntimeConfig, video: WebVideo) -> VideoDownload:
    channel = YtdlpChannel(config)
    candidate = Candidate(
        channel="ytdlp", title=video.title or video.url, size=0, protocol="web",
        ref={"url": video.url},
    )
    job_ref = await channel.download(candidate, WEB_VIDEO_NAMESPACE)
    dl = VideoDownload(kind="web_video", web_video_id=video.id, channel="ytdlp",
                  release_title=video.title or video.url, job_ref=job_ref,
                  state="downloading")
    session.add(dl)
    await session.commit()
    return dl
