import re

import pydantic
import pytest
from sqlalchemy import select

from conftest import run
from opus import db
from opus.config import Settings
from opus.models import Setting
from opus.music.channels import sabnzbd, slskd
from opus.settings_store import (
    SETTINGS_SPEC,
    SettingsValidationError,
    current_runtime,
    store_credentials,
    update_settings,
)
from opus.video.pipeline import grab as video_grab


async def _update(values: dict[str, str]):
    await update_settings(values)


async def _stored() -> dict[str, str]:
    async with db.SessionLocal() as session:
        return {s.key: s.value for s in (await session.execute(select(Setting))).scalars()}


@pytest.mark.parametrize(("key", "value", "code"), [
    ("music_dir", "/does/not/exist", "not_a_directory"),
    ("music_dir", "relative", "not_a_directory"),
    ("faces_threshold", "half", "not_a_number"),
    ("release_filter", "singles", "bad_value"),
    ("music_naming", "{artist}/{album}", "bad_template"),
    ("music_naming", "{artist}/{title}/{mood}", "bad_template"),
    ("channel_order", "slskd,slskd", "bad_value"),
    ("channel_order", "slskd,napster", "bad_value"),
    ("subtitle_langs", "en,HR", "bad_value"),
    ("category_movies", "films 4k", "bad_value"),
    ("category_movies", "Movies", "bad_value"),
    ("category_tv", "tv.hd", "bad_value"),
    ("category_tv", "tv\n", "bad_value"),
    ("bitrate_floor", "1080p=4", "bad_value"),
])
def test_a_bad_value_is_refused_and_nothing_is_written(clean, key, value, code):
    with pytest.raises(SettingsValidationError) as refused:
        run(_update({"subtitle_mode": "all", key: value}))
    assert (refused.value.key, refused.value.code) == (key, code)
    assert run(_stored()) == {}


def test_good_values_are_written_and_read_back(clean, tmp_path):
    run(_update({"music_dir": str(tmp_path), "faces_threshold": "0.55",
                 "episode_naming": "{series}/S{ss}E{ee}", "channel_order": "sabnzbd,slskd",
                 "bitrate_ceiling": "1080p:20,2160p:60.5", "prefer_hdr": "false",
                 "category_movies": "movies_4k", "category_tv": "tv-hd"}))

    runtime = run(current_runtime())
    assert runtime.get("music_dir") == str(tmp_path)
    assert runtime.float("faces_threshold") == 0.55
    assert runtime.rates("bitrate_ceiling") == {"1080p": 20.0, "2160p": 60.5}
    assert runtime.bool("prefer_hdr") is False
    assert (runtime.get("category_movies"), runtime.get("category_tv")) == ("movies_4k", "tv-hd")


def test_a_blank_secret_leaves_it_alone(clean):
    run(_update({"tmdb_api_key": "kept"}))
    run(_update({"tmdb_api_key": ""}))
    assert run(_stored())["tmdb_api_key"] == "kept"


@pytest.mark.parametrize(("key", "code"), [
    ("access_player_token", "not_editable"),
    ("library_ignored", "unknown_key"),
    ("no_such_setting", "unknown_key"),
])
def test_what_only_its_own_route_writes_is_refused(clean, key, code):
    with pytest.raises(SettingsValidationError) as refused:
        run(_update({key: "chosen-by-the-caller"}))
    assert (refused.value.key, refused.value.code) == (key, code)
    assert run(_stored()) == {}


def test_the_token_route_writes_the_token(clean):
    async def scenario():
        await store_credentials({"access_player_token": "minted"})
        with pytest.raises(SettingsValidationError):
            await store_credentials({"no_such_setting": "x"})
    run(scenario())
    assert run(_stored()) == {"access_player_token": "minted"}


def test_a_short_session_key_is_refused(monkeypatch):
    monkeypatch.setenv("OPUS_SESSION_KEY", "x" * 31)
    with pytest.raises(pydantic.ValidationError):
        Settings()
    monkeypatch.setenv("OPUS_SESSION_KEY", "x" * 32)
    assert Settings().session_key == "x" * 32


def test_every_namespace_the_library_grabs_under_is_one_downloads_accepts():
    namespaces = [sabnzbd.NAMESPACE, slskd.NAMESPACE, video_grab.WEB_VIDEO_NAMESPACE]
    namespaces += [s.default for s in SETTINGS_SPEC if s.key in ("category_movies", "category_tv")]
    assert all(re.fullmatch(r"[a-z0-9_-]+", n) for n in namespaces)

