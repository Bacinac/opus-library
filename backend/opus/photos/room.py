import os
from pathlib import Path

# What is left alone no matter who is sending. A photograph tree or a vault that
# fills its disk does not merely stop working: where it shares a pool with
# everything else on the box it takes the databases down with it. Refusing a
# file is a bad evening for one person; a full pool is a bad evening for the
# household.
KEEP_FREE = 20 * 1024**3

# The largest single file either door takes. Room alone is no ceiling: a pool
# with a terabyte free would accept a terabyte from one request. The largest
# picture or recording in the library so far is 666 MB; this is ten times that
# with room for a long 4K clip from a phone.
LARGEST = 8 * 1024**3


def spare(folder: Path) -> int:
    """Bytes that may still be written under this folder, after the reserve.
    Negative when the reserve has already been eaten into."""
    folder.mkdir(parents=True, exist_ok=True)
    stat = os.statvfs(folder)
    return stat.f_bavail * stat.f_frsize - KEEP_FREE
