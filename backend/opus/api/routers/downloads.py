"""One queue for the whole library.

This is the endpoint the merge earns outright. Once both halves acquire through
OPUS · Downloads, a record and a film in flight are the same fact — something
was asked for, it is this far along, it came from that engine — and there is no
reason to make anyone look in two places for it.

What is deliberately NOT averaged is the state word. Music settles into
`complete` or `rejected`; video can sit in `waiting_subtitles`, which has no
meaning for an album. Each row carries its own half's vocabulary and its own
half's endpoints for acting on it; only the list is shared."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from opus.db import get_session
from opus.models import (
    Episode,
    MusicDownload,
    Release,
    Season,
    VideoDownload,
)
from opus.music.pipeline import state as music_state

router = APIRouter()

LIMIT = 200

_VIDEO_TYPE = {"movie": "movies", "episode": "series", "web_video": "video"}


def _episode_label(episode: Episode) -> str:
    series = episode.season.series.title
    return f"{series} S{episode.season.number:02d}E{episode.number:02d}"


@router.get("/downloads")
async def list_downloads(session: AsyncSession = Depends(get_session)):
    music = await session.execute(
        select(MusicDownload)
        .options(selectinload(MusicDownload.release).selectinload(Release.artist))
        .order_by(MusicDownload.created_at.desc())
        .limit(LIMIT)
    )
    video = await session.execute(
        select(VideoDownload)
        .options(
            selectinload(VideoDownload.movie),
            selectinload(VideoDownload.episode)
            .selectinload(Episode.season)
            .selectinload(Season.series),
            selectinload(VideoDownload.web_video),
        )
        .order_by(VideoDownload.created_at.desc())
        .limit(LIMIT)
    )

    rows = [
        {
            "domain": "music",
            # the badge a row wears is the same word the library's type strip
            # uses, because they name the same thing
            "type": "music",
            "id": d.id,
            "wanted": f"{d.release.artist.name} — {d.release.title}",
            "release": d.release.title,
            "channel": d.channel,
            "state": d.status,
            "progress": (music_state.live.get(d.id) or {}).get("progress"),
            "detail": (music_state.live.get(d.id) or {}).get("detail") or d.error or "",
            "files": (music_state.live.get(d.id) or {}).get("files") or [],
            "created_at": d.created_at.isoformat(),
        }
        for d in music.scalars()
    ]
    rows += [
        {
            "domain": "video",
            "type": _VIDEO_TYPE.get(d.kind, "video"),
            "id": d.id,
            "wanted": (
                d.movie.title if d.movie is not None
                else _episode_label(d.episode) if d.episode is not None
                else d.web_video.title if d.web_video is not None
                else d.release_title
            ),
            "release": d.release_title,
            "channel": d.channel,
            "state": d.state,
            "progress": d.progress,
            "detail": d.detail,
            "files": [],
            "created_at": d.created_at.isoformat(),
        }
        for d in video.scalars()
    ]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows[:LIMIT]
