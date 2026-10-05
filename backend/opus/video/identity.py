import re
from functools import lru_cache
from pathlib import Path

from guessit import guessit

EPISODE_MARKER = re.compile(r"s\d+e\d+|\d+x\d+|(?<![a-z0-9])(?:episode|ep|e)[ ._-]*\d+", re.I)


@lru_cache(maxsize=512)
def episode_numbers(name: str) -> tuple[int | None, frozenset[int]]:
    parsed = guessit(name, {"type": "episode"})
    season = parsed.get("season")
    numbers = parsed.get("episode")
    if isinstance(numbers, int):
        numbers = [numbers]
    episodes = frozenset(n for n in (numbers or []) if isinstance(n, int))
    return season if isinstance(season, int) else None, episodes


def identify_episode(path: Path, root: Path) -> tuple[int | None, frozenset[int]]:
    own = episode_numbers(path.name)
    if own[0] is not None and own[1]:
        return own
    return episode_numbers(f"{root.name}/{path.relative_to(root).as_posix()}")
