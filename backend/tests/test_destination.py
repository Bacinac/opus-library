import errno
import logging
import os
from types import SimpleNamespace

import pytest

from opus import destination


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    monkeypatch.setattr(destination, "_refused", {})
    music = tmp_path / "music"
    music.mkdir()
    return music


def _wrapped(error: OSError) -> Exception:
    try:
        try:
            raise error
        except OSError as cause:
            raise RuntimeError("tag write failed") from cause
    except RuntimeError as outer:
        return outer


def test_a_write_the_shelf_refuses_is_the_shelfs_refusal(shelf, tmp_path):
    source = tmp_path / "landing" / "job"
    source.mkdir(parents=True)
    (source / "01.flac").write_bytes(bytes(1000))
    error = OSError(errno.EROFS, "Read-only file system", str(shelf / ".opus-imports" / "a"))

    refusal = destination.refusal_of(_wrapped(error), shelf, source)

    assert (refusal.shelf, refusal.path, refusal.code, refusal.room) == (
        shelf, shelf / ".opus-imports" / "a", errno.EROFS, 1000)
    assert f"the shelf {shelf} is read-only" in str(refusal)


@pytest.mark.parametrize("error", [
    OSError(errno.EROFS, "Read-only file system", "/landing/job/01.flac"),
    OSError(errno.EACCES, "Permission denied"),
    OSError(errno.EIO, "Input/output error", "/music/a"),
    OSError("second track cannot be tagged"),
    ValueError("not an OSError at all"),
])
def test_every_other_failure_is_not_the_shelfs(shelf, error):
    assert destination.refusal_of(error, shelf, None) is None


def test_a_full_disk_that_names_no_path_is_still_the_shelf(shelf):
    refusal = destination.refusal_of(OSError(errno.ENOSPC, "No space left on device"),
                                     shelf, None)
    assert (refusal.path, refusal.code, refusal.room) == (shelf, errno.ENOSPC, 0)


def test_a_rename_into_the_shelf_counts_by_its_second_path(shelf):
    error = OSError(errno.EACCES, "Permission denied", "/landing/job/01.flac", None,
                    str(shelf / "Artist" / "01.flac"))
    assert destination.refusal_of(error, shelf, None).path == shelf / "Artist" / "01.flac"


def test_a_refusal_is_said_once_and_stands_while_the_shelf_is_read_only(
        shelf, monkeypatch, caplog):
    monkeypatch.setattr(os, "statvfs", lambda path: SimpleNamespace(
        f_flag=os.ST_RDONLY, f_bavail=1 << 30, f_frsize=4096))
    error = OSError(errno.EROFS, "Read-only file system", str(shelf / "a"))
    with caplog.at_level(logging.ERROR, logger="opus.destination"):
        destination.refuse(destination.refusal_of(error, shelf, None))
        destination.refuse(destination.refusal_of(error, shelf, None))

    assert destination.refused(str(shelf)) is not None
    assert len(caplog.records) == 1


def test_a_full_shelf_stands_until_there_is_room_for_what_it_refused(shelf, caplog):
    error = OSError(errno.ENOSPC, "No space left on device", str(shelf / "a"))
    refusal = destination.refusal_of(error, shelf, None)
    refusal.room = 1 << 62
    destination.refuse(refusal)
    assert destination.refused(shelf) is not None

    destination._refused[shelf].room = 0
    with caplog.at_level(logging.WARNING, logger="opus.destination"):
        assert destination.refused(shelf) is None
    assert "takes files again" in caplog.text
    assert destination.refused(shelf) is None


def test_a_shelf_that_is_gone_does_not_take_files(shelf):
    error = OSError(errno.EROFS, "Read-only file system", str(shelf / "a" / "b"))
    destination.refuse(destination.refusal_of(error, shelf, None))
    shelf.rmdir()
    assert destination.refused(shelf) is not None
