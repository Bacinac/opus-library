"""Putting the shelf where the naming template says it goes.

Everything imported from now on is written at the path `episode_naming` renders,
so the shape of the library is settled — a season folder per season, and one
filename form inside it. What was adopted from a scan was written down where it
already lay, and the shapes it came in are still there: episodes loose in the
series folder with no season at all, `Season 4` beside `Season 04`, and scene
names beside rendered ones.

The template is the one definition of where an episode belongs, so this asks it
rather than restating it. A move carries the video's own siblings with it — the
sidecar subtitles and the tracks extracted out of the container, which are named
after the video and are lost the moment it is renamed without them. Nothing else
in the folder is touched: `folder.jpg`, `logo.png` and `seasonNN-poster.jpg`
describe the series, and the series folder is where they belong.

Nothing is overwritten. A target that already holds a different file is reported
and skipped, which is what two versions of one episode look like from here.

    docker compose exec backend python -m opus.video.relocate
    docker compose exec backend python -m opus.video.relocate --write
    docker compose exec backend python -m opus.video.relocate --folders-only
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from opus.db import SessionLocal
from opus.models import Episode, Season, Subtitle, VideoFile
from opus.settings_store import current_runtime
from opus.video.pipeline.naming import render_episode_path
from opus.video.subtitles.probe import VIDEO_EXTENSIONS


def _siblings(video: Path) -> list[Path]:
    """What is named after this video and means nothing without it.

    Directories as much as files: a player's scrubbing thumbnails arrive as
    `<stem>.trickplay/`, and a rule that took only files walked past eighty of
    them and left them behind in the folder the episode had just left.

    Another video is never one of them, and a name belongs to the video with the
    longest stem it starts with: `x.mkv` beside `x.mp4` or `x.part2.mkv` are
    three catalogued files, and carrying one inside another's move leaves two
    rows pointing at nothing. A name two videos claim equally stays put."""
    if not video.parent.is_dir():
        return []
    present = list(video.parent.iterdir())
    videos = {p for p in present if p.suffix.lower() in VIDEO_EXTENSIONS} | {video}
    prefix = video.stem + "."

    def owned(name: str) -> bool:
        claims = [len(v.stem) for v in videos if name.startswith(v.stem + ".")]
        return claims.count(len(video.stem)) == 1 and max(claims) == len(video.stem)

    return sorted(p for p in present
                  if p not in videos and p.name.startswith(prefix) and owned(p.name))


def _plan_one(config, episode: Episode, media: VideoFile, folders_only: bool):
    current = Path(media.path)
    rendered = render_episode_path(config, episode, current.suffix)
    target = rendered.parent / current.name if folders_only else rendered
    if target == current:
        return None
    moves = [(current, target)]
    for sibling in _siblings(current):
        moves.append((sibling, target.parent / (target.stem + sibling.name[len(current.stem):])))
    return moves


async def collect(folders_only: bool):
    async with SessionLocal() as session:
        config = await current_runtime()
        result = await session.execute(
            select(VideoFile).where(VideoFile.episode_id.is_not(None))
            .options(selectinload(VideoFile.episode).selectinload(Episode.season)
                     .selectinload(Season.series))
            .order_by(VideoFile.path))
        plans = []
        for media in result.scalars():
            moves = _plan_one(config, media.episode, media, folders_only)
            if moves:
                plans.append((media.id, moves))
        return plans


async def apply(plans) -> tuple[int, list[str]]:
    moved, refused = 0, []
    async with SessionLocal() as session:
        for media_id, moves in plans:
            video_from, video_to = moves[0]
            clash = next((dst for _, dst in moves if dst.exists()), None)
            if clash is not None:
                refused.append(f"{video_from} -> {video_to} ({clash.name} is already there)")
                continue
            if not video_from.exists():
                refused.append(f"{video_from} (gone from disk)")
                continue
            video_to.parent.mkdir(parents=True, exist_ok=True)
            for src, dst in moves:
                src.rename(dst)
            media = await session.get(VideoFile, media_id)
            media.path = str(video_to)
            subs = (await session.execute(
                select(Subtitle).where(Subtitle.file_id == media_id))).scalars()
            relocated = {str(src): str(dst) for src, dst in moves}
            for sub in subs:
                sub.path = relocated.get(sub.path, sub.path)
                sub.vtt_path = relocated.get(sub.vtt_path, sub.vtt_path)
            await session.commit()
            moved += 1
    return moved, refused


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="carry out the moves (otherwise only say what they would be)")
    parser.add_argument("--folders-only", action="store_true",
                        help="move into the season folder but keep the filename as it is")
    args = parser.parse_args()

    plans = await collect(args.folders_only)
    carried = sum(len(moves) - 1 for _, moves in plans)
    print(f"{len(plans)} episodes to move, carrying {carried} subtitle files")
    for _, moves in plans[:10]:
        print(f"  {moves[0][0]}\n  -> {moves[0][1]}")
    if len(plans) > 10:
        print(f"  ... and {len(plans) - 10} more")

    if not args.write:
        print("\nnothing written — pass --write to carry it out")
        return
    moved, refused = await apply(plans)
    print(f"\nmoved {moved} episodes")
    for line in refused:
        print(f"  refused: {line}")


if __name__ == "__main__":
    asyncio.run(main())
