import json
import subprocess
import sys

import pytest
from sqlalchemy import select

from conftest import run
from opus import db, importing
from opus.models import FileImport, Setting
from opus.music.library.inventory import collect_album_dirs
from opus.video.subtitles.probe import find_video_files


CHILD = """
import asyncio, json, os, sys
from pathlib import Path
from sqlalchemy import update
from opus import db, importing
from opus.models import FileImport, Setting

async def main():
    root, stop, committed = json.loads(sys.argv[1])
    root = Path(root)
    files = importing.Transaction(root / 'shelf')
    files.copy(root / 'input/a', root / 'shelf/a')
    files.copy(root / 'input/b', root / 'shelf/b')
    files.copy(root / 'input/c', root / 'shelf/c')
    files.retire(root / 'shelf/stale')
    original = importing._replace
    count = 0
    def replace(source, dest):
        nonlocal count
        original(source, dest)
        count += 1
        if count == stop:
            os._exit(73)
    importing._replace = replace
    await files.publish()
    if committed:
        async with db.SessionLocal() as session:
            await session.execute(update(Setting).where(Setting.key == 'test_catalogue')
                                  .values(value='new'))
            await session.execute(update(FileImport).where(FileImport.id == files.id)
                                  .values(committed=True))
            await session.commit()
    os._exit(73)
asyncio.run(main())
"""


@pytest.mark.parametrize("stop,committed", [(n, False) for n in range(1, 7)] + [(0, True)])
def test_process_death_recovers_files_and_catalogue_together(clean, tmp_path, stop, committed):
    (tmp_path / "input").mkdir()
    (tmp_path / "shelf").mkdir()
    for name in "abc":
        (tmp_path / "input" / name).write_bytes(f"new {name}".encode())
    for name in ("a", "b", "stale"):
        (tmp_path / "shelf" / name).write_bytes(f"old {name}".encode())

    async def seed():
        async with db.SessionLocal() as session:
            session.add(Setting(key="test_catalogue", value="old"))
            await session.commit()

    run(seed())
    result = subprocess.run([sys.executable, "-c", CHILD,
                             json.dumps([str(tmp_path), stop, committed])],
                            capture_output=True, text=True)
    assert result.returncode == 73, result.stderr
    run(importing.recover())
    run(importing.recover())

    async def outcome():
        async with db.SessionLocal() as session:
            return (await session.get(Setting, "test_catalogue")).value, (
                await session.execute(select(FileImport))).scalars().all()

    value, journals = run(outcome())
    assert value == ("new" if committed else "old")
    assert journals == []
    for name in "abc":
        assert (tmp_path / "input" / name).read_bytes() == f"new {name}".encode()
    for name in "ab":
        assert (tmp_path / "shelf" / name).read_bytes() == f"{'new' if committed else 'old'} {name}".encode()
    assert (tmp_path / "shelf/c").exists() is committed
    assert (tmp_path / "shelf/stale").exists() is not committed


def test_scans_ignore_prepared_and_retained_import_files(tmp_path):
    files = importing.Transaction(tmp_path)
    for name in ("song.flac", "film.mkv"):
        staged = files.reserve(tmp_path / "album" / name)
        staged.write_bytes(b"prepared")
        (staged.parent / "previous").write_bytes(b"retained")
    assert collect_album_dirs(tmp_path) == {}
    assert find_video_files(tmp_path) == []


def test_preparation_left_by_a_dead_process_is_removed_before_scans(tmp_path):
    files = importing.Transaction(tmp_path)
    files.reserve(tmp_path / "album" / "song.flac").write_bytes(b"partial copy")
    unknown = tmp_path / importing.STAGING / "unowned"
    unknown.mkdir()
    importing._clean_orphans(tmp_path)
    assert unknown.exists()
    assert not unknown.with_name(files.id).exists()


def test_a_lost_commit_acknowledgement_does_not_restore_old_files(clean, tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    source, dest = tmp_path / "source", tmp_path / "dest"
    source.write_bytes(b"new")
    dest.write_bytes(b"old")
    original = AsyncSession.commit

    async def commit(session):
        await original(session)
        raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(AsyncSession, "commit", commit)

    async def scenario():
        async with db.SessionLocal() as session:
            importing.transaction(session, tmp_path).copy(source, dest)
            session.add(Setting(key="test_catalogue", value="new"))
            await importing.commit(session)
        async with db.SessionLocal() as session:
            assert (await session.get(Setting, "test_catalogue")).value == "new"
            assert (await session.execute(select(FileImport))).scalars().all() == []

    run(scenario())
    assert source.read_bytes() == b"new"
    assert dest.read_bytes() == b"new"
