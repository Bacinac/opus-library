"""What a file is called once it lands on the shelf."""

import re
from pathlib import Path

from opus.models import Episode, Movie, WebVideo
from opus.settings_store import RuntimeConfig


def render_movie_path(config: RuntimeConfig, movie: Movie, ext: str) -> Path:
    rendered = config.get("movie_naming").format(
        title=safe(movie.title), year=movie.year or "")
    return Path(config.get("movies_dir")) / f"{rendered.strip()}{ext}"


def render_episode_path(config: RuntimeConfig, episode: Episode, ext: str) -> Path:
    """Where the template says this episode goes.

    The separators in the template are the template's own; the ones inside a
    title are not. `Love/Addiction` and `Career Day (1) / Career Day (2)` are
    episode titles, and substituted raw they would each open a folder that was
    never asked for and leave the episode under half its own name."""
    series = episode.season.series
    rendered = config.get("episode_naming").format(
        series=safe(series.title), year=series.year or "",
        ss=f"{episode.season.number:02d}", ee=f"{episode.number:02d}",
        title=safe(episode.title))
    return Path(config.get("tv_dir")) / f"{rendered.strip()}{ext}"


def render_web_path(config: RuntimeConfig, video: WebVideo, source_name: str) -> Path:
    folder = video.channel.title if video.channel else (video.uploader or "Videos")
    return Path(config.get("video_dir")) / folder / source_name


# What a name may not contain. Not a Linux rule — only the slash is that — but a
# Windows one, and the library is served over SMB, so a colon in a folder name
# is a folder no Windows client can open. `Monarch: Legacy of Monsters` and
# `The End of the F***ing World` are titles TMDB gives us and names Windows
# refuses; the shelf already held them as `Monarch - Legacy of Monsters` and
# `The End of the F-ing World`, which is the mapping below and not a coincidence.
# Two kinds of forbidden. One stands for something and leaves a dash behind —
# `F***ing` has always been `F-ing` on this shelf — and one is punctuation the
# name reads fine without.
_FORBIDDEN = str.maketrans({**{c: "-" for c in '<>:/\\|*'}, **{c: "" for c in '?"'}})


def safe(component: str) -> str:
    # a colon separating a title from its subtitle reads as a dash with air
    # around it, which is how the shelf already spells these
    text = component.replace(": ", " - ").translate(_FORBIDDEN)
    # runs collapse so that `F***ing` becomes `F-ing` rather than `F---ing`, and
    # a name may end in neither a dot, a space nor a dash left by the above
    return re.sub(r"-{2,}", "-", text).strip().rstrip(". -")
