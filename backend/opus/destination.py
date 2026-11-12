"""Whether a shelf can take what a download brings.

A shelf that refuses a write is not a bad source: every other source fails the
same way, and each attempt costs a whole album or film over the wire. A read-only
music library once had one album fetched 192 times in a week. The shelf's own
refusal therefore stops acquisition for that shelf, said once, and the download
that met it waits in the landing zone instead of failing. Each later ask looks
at the shelf again, so a remount or freed space resumes acquisition without a
restart.
"""

import errno
import logging
import os
from pathlib import Path

from opus import landing

log = logging.getLogger("opus.destination")

_REASONS = {
    errno.EROFS: "is read-only",
    errno.ENOSPC: "is full",
    errno.EDQUOT: "is over its quota",
    errno.EACCES: "does not let OPUS write",
}


class Refused(Exception):
    def __init__(self, shelf: Path, path: Path, code: int, room: int):
        self.shelf, self.path, self.code, self.room = shelf, path, code, room
        super().__init__(f"the shelf {shelf} {_REASONS[code]} ({path}); "
                         "nothing more is acquired for it until it takes files again")


_refused: dict[Path, Refused] = {}


def _within(path: Path, shelf: Path) -> bool:
    return path == shelf or shelf in path.parents


def refusal_of(exc: BaseException, shelf: Path, source: Path | None) -> Refused | None:
    """The shelf's own refusal anywhere in the chain of `exc`, or None for every
    other failure. A write that names a path outside the shelf is the landing
    zone's or the source's business; a permission error that names no path at
    all cannot be told apart from a source the import may not read.

    `source` is what the refused import was carrying: its size is the room the
    shelf must have before a full shelf counts as taking files again."""
    shelf = Path(os.path.abspath(shelf))
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, OSError) and cause.errno in _REASONS:
            named = [Path(os.path.abspath(os.fsdecode(name)))
                     for name in (cause.filename, cause.filename2) if name is not None]
            inside = [path for path in named if _within(path, shelf)]
            if inside or (not named and cause.errno != errno.EACCES):
                room = landing.size(source) if source is not None and source.exists() else 0
                return Refused(shelf, inside[0] if inside else shelf, cause.errno, room)
            return None
        cause = cause.__cause__ or cause.__context__
    return None


def refuse(refusal: Refused) -> None:
    if refusal.shelf not in _refused:
        log.error("%s", refusal)
    else:
        refusal.room = max(refusal.room, _refused[refusal.shelf].room)
    _refused[refusal.shelf] = refusal


def refused(shelf: Path | str) -> Refused | None:
    """The refusal that still stands for this shelf, after looking at it again."""
    shelf = Path(os.path.abspath(shelf))
    refusal = _refused.get(shelf)
    if refusal is None or not _takes(refusal):
        return refusal
    del _refused[shelf]
    log.warning("the shelf %s takes files again; acquisition for it resumes", shelf)
    return None


def _takes(refusal: Refused) -> bool:
    where = refusal.path
    while not where.is_dir():
        if where == refusal.shelf or where.parent == where:
            return False
        where = where.parent
    try:
        stat = os.statvfs(where)
    except OSError:
        return False
    if stat.f_flag & os.ST_RDONLY or not os.access(where, os.W_OK):
        return False
    return stat.f_bavail * stat.f_frsize > refusal.room
