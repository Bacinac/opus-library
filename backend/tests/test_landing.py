from conftest import library, run, signed_in
from opus import db
from opus.landing import active_folders, candidates, preview, without_active
from opus.models import VideoDownload


def test_candidates_walk_through_nested_engine_namespaces(tmp_path):
    root = tmp_path / "landing"
    expected = [
        root / "torrent" / "An-album",
        root / "stream" / "music" / "A-streamed-album",
        root / "newsgroup" / "complete" / "music" / "A-posted-album",
    ]
    for folder in expected:
        folder.mkdir(parents=True)
        (folder / "track.flac").write_bytes(b"audio")
    (root / ".incomplete" / "not-yet").mkdir(parents=True)

    assert candidates(root) == sorted(expected)


def test_active_landing_folder_and_its_children_are_excluded(tmp_path):
    root = tmp_path / "landing"
    active = root / "torrent" / "Still-downloading"
    nested = active / "disc-1"
    abandoned = root / "torrent" / "Left-behind"
    for folder in (nested, abandoned):
        folder.mkdir(parents=True)

    assert without_active([active, nested, abandoned], {active}) == [abandoned]


def test_active_folders_reads_the_path_of_an_inflight_video_download(clean, tmp_path):
    root = tmp_path / "landing"
    held = root / "stream" / "movie"

    async def scenario():
        async with db.SessionLocal() as session:
            session.add(VideoDownload(kind="movie", channel="stream", state="downloading",
                                      job_ref={"landing": str(held)}))
            await session.commit()
            return await active_folders(session, root)

    assert run(scenario()) == {held.resolve()}


def test_preview_reports_only_old_unheld_candidates_and_never_removes_them(tmp_path):
    root = tmp_path / "landing"
    stale = root / "torrent" / "Old-album"
    held = root / "torrent" / "Still-downloading"
    fresh = root / "torrent" / "New-album"
    for folder, contents in ((stale, b"old"), (held, b"held"), (fresh, b"new")):
        folder.mkdir(parents=True)
        (folder / "track.flac").write_bytes(contents)
    # Use an old mtime on both the file and directory: _idle_days intentionally
    # follows the file timestamp rather than trusting the directory alone.
    import os
    import time
    old = time.time() - 3 * 86400
    os.utime(stale / "track.flac", (old, old))
    os.utime(stale, (old, old))
    os.utime(held / "track.flac", (old, old))
    os.utime(held, (old, old))

    said = run(preview(root, keep=2, active={held}))

    assert said["candidates"] == 3
    assert said["protected"] == 1
    assert said["stale"] == 1
    assert said["reclaimable"] == 3
    assert said["entries"] == [{"path": "torrent/Old-album", "idle_days": 3.0, "bytes": 3}]
    assert stale.is_dir() and held.is_dir() and fresh.is_dir()


def test_candidates_never_follow_a_symlink_out_of_the_landing_zone(tmp_path):
    root = tmp_path / "landing"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "do-not-touch.flac").write_bytes(b"outside")
    (root / "torrent").mkdir(parents=True)
    (root / "torrent" / "elsewhere").symlink_to(outside, target_is_directory=True)

    assert candidates(root) == []


def test_preview_endpoint_is_read_only_and_reports_the_next_sweep(clean, monkeypatch, tmp_path):
    root = tmp_path / "landing"
    old = root / "torrent" / "Old-album"
    old.mkdir(parents=True)
    song = old / "track.flac"
    song.write_bytes(b"audio")
    import os
    import time
    then = time.time() - 15 * 86400
    os.utime(song, (then, then))
    os.utime(old, (then, then))

    async def known_root():
        return root

    monkeypatch.setattr("opus.landing.root", known_root)

    async def scenario():
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return await client.get("/api/landing")

    answer = run(scenario())
    assert answer.status_code == 200
    assert answer.json()["entries"] == [{
        "path": "torrent/Old-album", "idle_days": 15.0, "bytes": 5,
    }]
    assert old.is_dir()
