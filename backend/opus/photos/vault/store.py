"""Where a vault's bytes sit, and how an interrupted upload carries on.

One file per picture, named by its id, under a directory per person. No tree
shaped after dates or albums, because the server does not know either — a vault
is opaque to the machine holding it, and a layout that grouped things would be
the server knowing something it should not.

Appending is the whole protocol. A phone says how far it got, the server says
how far it actually has, and the phone carries on from there. Anything else
would mean a forty-megabyte video that lost its connection at thirty starts
again from nothing, which on a household connection is how a backup quietly
stops happening."""

import os
from pathlib import Path

from opus.photos import room

# what a client is asked to encrypt in one piece. Large enough that the overhead
# of a tag per chunk is nothing, small enough that a phone can hold one in
# memory while it works and a resume never loses much.
CHUNK = 4 * 1024 * 1024

def spare(config) -> int:
    return room.spare(root(config))


def room_for(config, wanted: int) -> bool:
    return spare(config) >= wanted


def root(config) -> Path:
    return Path(config.get("photos_vault_dir"))


def theirs(config, person: str) -> Path:
    """A person's own corner. The name is from the roster and has already been
    tidied to one spelling, but it still gets checked here: a directory is a
    path, and a path is the one place a name must not be creative."""
    if not person or "/" in person or person.startswith("."):
        raise ValueError(f"not a name a directory can be made from: {person!r}")
    return root(config) / person


def where(config, person: str, file_id: str) -> Path:
    return theirs(config, person) / file_id


def thumb_at(config, person: str, file_id: str) -> Path:
    return theirs(config, person) / f"{file_id}.t"


def held(config, person: str, file_id: str) -> int:
    """How much of this file the disk actually has.

    Asked of the disk rather than of the row on purpose: the row is what we
    believe and the file is what is true, and a server that fell over between
    the write and the commit must resume from the smaller of the two."""
    try:
        return where(config, person, file_id).stat().st_size
    except FileNotFoundError:
        return 0


def append(config, person: str, file_id: str, at: int, data: bytes) -> int:
    """Add to the end, and only to the end.

    A write that does not start exactly where the file ends is refused rather
    than padded or seeked: out-of-order chunks are a bug in the client, and a
    file with a hole in it decrypts to nothing at all, which would be discovered
    much later and by the person who most needs it to work."""
    path = where(config, person, file_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = held(config, person, file_id)
    if at != size:
        raise ValueError(f"chunk starts at {at}, file ends at {size}")
    with open(path, "ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return size + len(data)


def discard(config, person: str, file_id: str) -> None:
    where(config, person, file_id).unlink(missing_ok=True)


def put_thumb(config, person: str, file_id: str, data: bytes):
    path = thumb_at(config, person, file_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def forget(config, person: str, file_id: str):
    for path in (where(config, person, file_id), thumb_at(config, person, file_id)):
        path.unlink(missing_ok=True)

