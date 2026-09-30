import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from conftest import run
from opus import db
from opus.models import Episode, Season, Series, Setting, Subtitle, VideoFile
from opus.video import relocate


@pytest.fixture
def shelf(tmp_path, clean):
    tv = tmp_path / "television"
    (tv / "Show").mkdir(parents=True)

    async def configure():
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(key="tv_dir", value=str(tv)))
            await session.commit()

    run(configure())
    return tv


async def _episode(session, number: int, title: str) -> int:
    series = (await session.scalars(select(Series).where(Series.tmdb_id == 1))).first()
    if series is None:
        series = Series(tmdb_id=1, title="Show", year=2020)
        session.add(series)
        await session.flush()
        session.add(Season(series_id=series.id, number=1))
        await session.flush()
    season = (await session.scalars(select(Season).where(Season.series_id == series.id))).one()
    episode = Episode(season_id=season.id, number=number, title=title)
    session.add(episode)
    await session.flush()
    return episode.id


def lay(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _catalogue(video, episode_id, subtitles=()) -> int:
    async with db.SessionLocal() as session:
        media = VideoFile(path=str(video), episode_id=episode_id)
        session.add(media)
        await session.flush()
        for fields in subtitles:
            session.add(Subtitle(file_id=media.id, **fields))
        await session.commit()
        return media.id


async def _state(media_id):
    async with db.SessionLocal() as session:
        media = await session.get(VideoFile, media_id)
        subs = (await session.execute(
            select(Subtitle.path, Subtitle.vtt_path).where(Subtitle.file_id == media_id)
            .order_by(Subtitle.id))).all()
        return media.path, [tuple(row) for row in subs]


def relocated(folders_only=False):
    async def go():
        return await relocate.apply(await relocate.collect(folders_only))

    return run(go())


def test_a_loose_episode_moves_into_its_season_with_everything_named_after_it(shelf):
    folder = shelf / "Show"
    video = lay(folder / "show.s01e03.mkv", b"picture")
    srt = lay(folder / "show.s01e03.en.srt", b"words")
    vtt = lay(folder / "show.s01e03.hr.0.opus.vtt", b"vtt")
    lay(folder / "show.s01e03.trickplay" / "320.jpg", b"strip")
    lay(folder / "folder.jpg", b"art")
    neighbour = lay(folder / "show.s01e04.mkv", b"next")

    async def seed():
        async with db.SessionLocal() as session:
            episode_id = await _episode(session, 3, "Pilot: Part 1")
            await session.commit()
        return await _catalogue(video, episode_id, [
            {"lang": "en", "source": "external", "format": "srt", "path": str(srt)},
            {"lang": "hr", "source": "embedded", "format": "subrip", "stream_index": 0,
             "vtt_path": str(vtt)},
            {"lang": "de", "source": "embedded", "format": "subrip", "stream_index": 1},
        ])

    media_id = run(seed())
    moved, refused = relocated()

    season = folder / "Season 01"
    target = season / "Show - S01E03 - Pilot - Part 1"
    assert (moved, refused) == (1, [])
    assert sorted(p.name for p in season.iterdir()) == [
        target.name + ".en.srt", target.name + ".hr.0.opus.vtt",
        target.name + ".mkv", target.name + ".trickplay"]
    assert (season / (target.name + ".trickplay") / "320.jpg").read_bytes() == b"strip"
    assert (season / (target.name + ".mkv")).read_bytes() == b"picture"
    assert sorted(p.name for p in folder.iterdir()) == ["Season 01", "folder.jpg", "show.s01e04.mkv"]
    assert neighbour.read_bytes() == b"next"
    assert run(_state(media_id)) == (str(season / (target.name + ".mkv")), [
        (str(season / (target.name + ".en.srt")), None),
        (None, str(season / (target.name + ".hr.0.opus.vtt"))),
        (None, None),
    ])


def test_folders_only_keeps_the_filename(shelf):
    video = lay(shelf / "Show" / "show.s01e01.mkv")
    srt = lay(shelf / "Show" / "show.s01e01.hr.srt")

    async def seed():
        async with db.SessionLocal() as session:
            episode_id = await _episode(session, 1, "One")
            await session.commit()
        return await _catalogue(video, episode_id, [
            {"lang": "hr", "source": "external", "format": "srt", "path": str(srt)}])

    media_id = run(seed())
    assert relocated(folders_only=True) == (1, [])
    season = shelf / "Show" / "Season 01"
    assert run(_state(media_id)) == (str(season / "show.s01e01.mkv"),
                                     [(str(season / "show.s01e01.hr.srt"), None)])


def test_an_episode_already_where_the_template_says_is_left_alone(shelf):
    video = lay(shelf / "Show" / "Season 01" / "Show - S01E02 - Two.mkv")

    async def seed():
        async with db.SessionLocal() as session:
            episode_id = await _episode(session, 2, "Two")
            await session.commit()
        return await _catalogue(video, episode_id)

    run(seed())
    assert run(relocate.collect(False)) == []


def test_a_target_already_taken_refuses_the_whole_move(shelf):
    video = lay(shelf / "Show" / "show.s01e02.mkv", b"mine")
    srt = lay(shelf / "Show" / "show.s01e02.en.srt", b"mine")
    lay(shelf / "Show" / "Season 01" / "Show - S01E02 - Two.en.srt", b"theirs")

    async def seed():
        async with db.SessionLocal() as session:
            episode_id = await _episode(session, 2, "Two")
            await session.commit()
        return await _catalogue(video, episode_id, [
            {"lang": "en", "source": "external", "format": "srt", "path": str(srt)}])

    media_id = run(seed())
    moved, refused = relocated()
    assert moved == 0 and len(refused) == 1 and "is already there" in refused[0]
    assert video.read_bytes() == b"mine" and srt.read_bytes() == b"mine"
    assert run(_state(media_id)) == (str(video), [(str(srt), None)])


def test_a_file_gone_from_disk_is_refused_and_its_row_kept(shelf):
    video = shelf / "Show" / "show.s01e05.mkv"

    async def seed():
        async with db.SessionLocal() as session:
            episode_id = await _episode(session, 5, "Five")
            await session.commit()
        return await _catalogue(video, episode_id)

    media_id = run(seed())
    assert relocated() == (0, [f"{video} (gone from disk)"])
    assert run(_state(media_id)) == (str(video), [])


def test_two_catalogued_videos_sharing_a_name_each_move_on_their_own(shelf):
    folder = shelf / "Show"
    mkv = lay(folder / "show.s01e06.mkv", b"mkv")
    mp4 = lay(folder / "show.s01e06.mp4", b"mp4")
    longer = lay(folder / "show.s01e06.part2.mkv", b"part2")
    longer_srt = lay(folder / "show.s01e06.part2.en.srt", b"part2 words")
    shared = lay(folder / "show.s01e06.hr.srt", b"whose")

    async def seed():
        async with db.SessionLocal() as session:
            six = await _episode(session, 6, "Six")
            seven = await _episode(session, 7, "Seven")
            eight = await _episode(session, 8, "Eight")
            await session.commit()
        return (await _catalogue(mkv, six), await _catalogue(mp4, seven),
                await _catalogue(longer, eight, [
                    {"lang": "en", "source": "external", "format": "srt",
                     "path": str(longer_srt)}]))

    six, seven, eight = run(seed())
    assert relocated() == (3, [])
    season = folder / "Season 01"
    assert run(_state(six)) == (str(season / "Show - S01E06 - Six.mkv"), [])
    assert run(_state(seven)) == (str(season / "Show - S01E07 - Seven.mp4"), [])
    assert run(_state(eight)) == (str(season / "Show - S01E08 - Eight.mkv"),
                                  [(str(season / "Show - S01E08 - Eight.en.srt"), None)])
    assert sorted(p.name for p in season.iterdir()) == [
        "Show - S01E06 - Six.mkv", "Show - S01E07 - Seven.mp4",
        "Show - S01E08 - Eight.en.srt", "Show - S01E08 - Eight.mkv"]
    assert (season / "Show - S01E07 - Seven.mp4").read_bytes() == b"mp4"
    assert sorted(p.name for p in folder.iterdir()) == ["Season 01", shared.name]
