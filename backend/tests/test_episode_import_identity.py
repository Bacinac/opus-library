from types import SimpleNamespace

import pytest

from opus.video.pipeline import importer


@pytest.mark.parametrize("names,chosen", [
    (["Show.S02E03.mkv", "Show.S01E03.mkv"], "Show.S01E03.mkv"),
    (["Show.S01E04.mkv", "Season 1/Episode 3.mkv"], "Season 1/Episode 3.mkv"),
    (["Show.S01E03E04.mkv"], "Show.S01E03E04.mkv"),
    (["Show.S02E03.mkv"], None),
    (["Show.S01E04.mkv"], None),
    (["video.mkv"], None),
])
def test_episode_selection_requires_the_target_season_and_number(tmp_path, names, chosen):
    root = tmp_path / "Show"
    episode = SimpleNamespace(number=3, season=SimpleNamespace(number=1))
    videos = [root / name for name in names]
    if chosen is None:
        with pytest.raises(importer.ImportFailure, match="S01E03"):
            importer._episode_file(videos, root, episode)
    else:
        assert importer._episode_file(videos, root, episode) == root / chosen
