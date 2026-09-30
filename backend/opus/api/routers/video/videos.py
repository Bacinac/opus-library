"""Web video: paste a URL and either the one video is fetched or the channel
behind it is followed, after which new uploads arrive on their own."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from opus.acquire import AcquireError
from opus.api.routers.video.shared import _playback_json
from opus.db import get_session
from opus.models import VideoDownload, VideoFile, WebChannel, WebVideo
from opus.settings_store import current_runtime
from opus.video.pipeline import grab, items
from opus.video.metadata.webvideo import probe_url

router = APIRouter()


class AddUrl(BaseModel):
    url: str


@router.get("/videos")
async def videos_list(session=Depends(get_session)):
    channels = await session.execute(select(WebChannel).order_by(WebChannel.added_at.desc()))
    videos = await session.execute(
        select(WebVideo).options(selectinload(WebVideo.files), selectinload(WebVideo.channel))
        .order_by(WebVideo.added_at.desc()))
    active = await items.active_ids(session, VideoDownload.web_video_id)
    return {
        "channels": [{
            "id": c.id, "url": c.url, "title": c.title, "thumb_url": c.thumb_url,
            "monitored": c.monitored,
        } for c in channels.scalars()],
        "videos": [{
            "id": v.id, "url": v.url, "title": v.title,
            "uploader": v.channel.title if v.channel else v.uploader,
            "thumb_url": v.thumb_url, "duration_s": v.duration_s,
            "status": ("complete" if v.files
                       else "downloading" if v.id in active else "wanted"),
        } for v in videos.scalars()],
    }


@router.get("/videos/{video_id}/playback")
async def video_playback(video_id: int, session=Depends(get_session)):
    """What /movies/{id}/playback answers, for something fetched off the web.
    A web video has no subtitle policy and no metadata worth speaking of — it is
    a file with a title, which is exactly what the player needs from it."""
    result = await session.execute(
        select(WebVideo).where(WebVideo.id == video_id).options(
            selectinload(WebVideo.channel),
            selectinload(WebVideo.files).selectinload(VideoFile.subtitles),
            selectinload(WebVideo.files).selectinload(VideoFile.streams),
                 selectinload(WebVideo.files).selectinload(VideoFile.chapters)))
    video = result.scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=404)
    if not video.files:
        raise HTTPException(status_code=409,
                            detail="nothing has been downloaded for this video")
    return {
        "title": video.title,
        "year": video.published_at.year if video.published_at else None,
        "runtime_min": None,
        "backdrop_url": video.thumb_url,
        "uploader": video.channel.title if video.channel else video.uploader,
        **_playback_json(video.files[0]),
    }


@router.post("/videos", status_code=201)
async def videos_add(body: AddUrl, session=Depends(get_session)):
    """Paste a URL: a single video is grabbed immediately; a channel/playlist
    is followed (new uploads auto-download; the listed backlog stays manual)."""
    config = await current_runtime()
    try:
        info = await probe_url(body.url)
    except AcquireError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if info.get("entries") is not None:
        existing = await session.execute(
            select(WebChannel).where(WebChannel.url == body.url))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="already followed")
        channel = WebChannel(
            url=body.url, external_id=info.get("id") or "",
            title=info.get("title") or info.get("channel") or body.url,
            thumb_url=(info.get("thumbnails") or [{}])[-1].get("url"))
        session.add(channel)
        await session.flush()
        for entry in info.get("entries") or []:
            if not entry.get("id"):
                continue
            session.add(WebVideo(
                channel_id=channel.id, external_id=entry["id"],
                url=entry.get("url") or entry.get("webpage_url") or "",
                title=entry.get("title", ""), uploader=channel.title,
                duration_s=int(entry["duration"]) if entry.get("duration") else None))
        await session.commit()
        return {"kind": "channel", "id": channel.id, "title": channel.title}

    existing = await session.execute(
        select(WebVideo).where(WebVideo.external_id == info["id"]))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="already added")
    video = WebVideo(
        external_id=info["id"], url=info.get("webpage_url") or body.url,
        title=info.get("title", ""), uploader=info.get("uploader", ""),
        thumb_url=info.get("thumbnail"),
        duration_s=int(info["duration"]) if info.get("duration") else None)
    session.add(video)
    await session.flush()
    dl = await grab.grab_web_video(session, config, video)
    return {"kind": "video", "id": video.id, "title": video.title, "download_id": dl.id}


@router.post("/videos/{video_id}/download", status_code=202)
async def video_download(video_id: int, session=Depends(get_session)):
    config = await current_runtime()
    video = await session.get(WebVideo, video_id)
    if video is None:
        raise HTTPException(status_code=404)
    dl = await grab.grab_web_video(session, config, video)
    return {"download_id": dl.id}


@router.delete("/videos/channels/{channel_id}", status_code=204)
async def channel_delete(channel_id: int, session=Depends(get_session)):
    await session.execute(delete(WebChannel).where(WebChannel.id == channel_id))
    await session.commit()
