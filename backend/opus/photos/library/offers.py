"""Offering a picture from a vault to the household.

Two stores, two owners, two lifetimes. The vault is one person's and unreadable
here; the library is the family's and read in full. A picture offered from one
to the other is **copied**, not moved — deliberately, because if offering
emptied the vault then a photograph deleted from the shared library a year later
would take somebody's only backup with it, and a backup with holes in it is not
one.

What arrives is a file in the waiting room, which is a folder inside the photo
tree, so the ordinary pass adopts it like anything else that appears there — there
is deliberately no second way into the catalogue. It waits there only until the
catalogue knows when the picture was taken; then `place` moves it to its year.
The year cannot be decided here: the date a phone puts on a file is the date it
was copied about, and the holiday in the photograph was six years ago.

Who offered it is remembered against the checksum rather than the path, because
the catalogue is content-addressed and the tree is going to be reshaped."""

import asyncio
import hashlib
import os
import re
import time
import unicodedata
import zlib
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from opus.models import Photo, PhotoFile, PhotoOffer, VaultFile

# What a name may be once it is ours. Letters are left alone — a household that
# writes "Ljetovanje u Šibeniku.jpg" should get that file back under that name,
# and an allowlist of ASCII would quietly rename half of them. What goes is what
# a path or a filesystem would read as an instruction: separators, control
# characters, and the handful Windows reserves.
TIDY = re.compile(r'[\x00-\x1f\x7f/\\<>:"|?*]+')
NAME_LIMIT = 120

# where an offered picture waits to be catalogued. A folder of the tree, so the
# pass sees it; not a year, because which year it belongs to is not yet known
WAITING = "inbox"

# what an offer writes before it knows what it holds, and how long one may sit
# untouched before it is taken for the leftover of an offer that never finished
UNFINISHED = (".arriving", ".opening")
STALE_AFTER = 3600

OFFERING = zlib.crc32(b"opus.photos.offering") - 2**31


def writable(config) -> Path:
    return Path(config.get("photos_write_dir"))


def waiting_room(config) -> Path:
    """Where an offered picture is written. The writable name."""
    return writable(config) / WAITING


def waiting_seen(config) -> Path:
    """The same folder under the name the catalogue records it by. Every path in
    the database is a read-only one, so that a path in a row is always the path
    the next pass will go looking for."""
    return Path(config.get("photos_dir")) / WAITING


def named(name: str) -> str:
    # an iPhone sends "Šibenik" as S + combining caron; every other device sends
    # the single letter. Composed here so two names that read the same are the
    # same name on disk.
    clean = unicodedata.normalize("NFC", name or "").strip()
    # no separator survives, so nothing that is left can climb out of the folder
    clean = TIDY.sub("_", clean).lstrip(".")
    return clean[-NAME_LIMIT:] or "photograph"


def _sha1(path: Path) -> bytes:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


async def _same_picture(source: Path, there: Path, checksum: bytes) -> bool:
    try:
        if os.path.samefile(source, there):
            return True
        if there.stat().st_size != source.stat().st_size:
            return False
    except FileNotFoundError:
        return False
    return await asyncio.to_thread(_sha1, there) == checksum


async def land(session, source: Path, folder: Path, seen: Path, name: str,
               checksum: bytes) -> Path:
    """Link a finished file to a name nothing claims, and leave the source for
    the caller to remove once what it means is written down. Two phones can
    offer IMG_0001.JPG on the same evening and neither of them is wrong.

    A link and not a rename, because a link refuses a name that is already
    there where a rename would replace it. `photo_files.path` is unique, so a
    name free on the disk may still belong to a row whose file has gone. A
    name already holding these very bytes and claimed by nothing is what an
    earlier landing left when it was cut off, and it is taken rather than
    copied beside."""
    stem, dot, suffix = name.rpartition(".")
    stem, suffix = (stem, f".{suffix}") if dot else (name, "")
    n = 0
    while True:
        tail = name if n == 0 else f"{stem}-{n}{suffix}"
        n += 1
        claimed = await session.scalar(
            select(PhotoFile.id).where(PhotoFile.path == str(seen / tail)))
        if claimed is not None:
            continue
        try:
            os.link(source, folder / tail)
        except FileExistsError:
            if await _same_picture(source, folder / tail, checksum):
                return folder / tail
            continue
        return folder / tail


async def _one_at_a_time(session, checksum: bytes) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:space, :key)"),
                          {"space": OFFERING,
                           "key": int.from_bytes(checksum[:4], "big", signed=True)})


def sweep(config) -> int:
    """Remove what offers began writing and never finished, once nothing has
    touched it for long enough that no request can still be writing it."""
    folder = waiting_room(config)
    if not folder.is_dir():
        return 0
    gone = 0
    cutoff = time.time() - STALE_AFTER
    for leftover in folder.iterdir():
        if not (leftover.name.startswith(".") and leftover.name.endswith(UNFINISHED)):
            continue
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
                gone += 1
        except FileNotFoundError:
            continue
    return gone


async def _link(session, vault_id: str | None, person: str, photo_id: int):
    """Say, on the vault's own row, that this one is in the library now."""
    if not vault_id:
        return
    row = await session.get(VaultFile, vault_id)
    if row is not None and row.person == person:
        row.photo_id = photo_id


async def already(session, checksum: bytes, person: str,
                  vault_id: str | None) -> dict | None:
    """Whether the household already has this, in either of the two ways it can.

    Asked before the decrypted file is put anywhere. Already there is the common
    case and not a failure: the same photograph reaches two phones through a
    chat, and the second offer of it should say so rather than leave a duplicate
    beside the first."""
    known = await session.scalar(select(Photo).where(Photo.checksum == checksum))
    if known is not None:
        if not known.offered_by:
            # it was in the tree already, but nobody had put their name to it
            known.offered_by = person
        await _link(session, vault_id, person, known.id)
        await session.commit()
        return {"known": True, "photo_id": known.id}

    if await session.get(PhotoOffer, checksum) is not None:
        # offered a minute ago and not yet adopted — the catalogue has nothing to
        # recognise it by yet, and a second copy would be two files the next pass
        # would have to reconcile
        return {"known": True, "pending": True}
    return None


async def give(session, source: Path, config, name: str, checksum: bytes,
               person: str, vault_id: str | None) -> Path | None:
    """Put an offered file in the waiting room under the name of whoever offered
    it, or None when the same picture is already being given by another call.

    The note and the landing are one transaction, held against the pass that
    adopts the file until the note can be read; the source goes only once that
    transaction is committed. Cut off anywhere before, the next offer of the
    same picture takes over whatever name was already linked."""
    await _one_at_a_time(session, checksum)
    session.add(PhotoOffer(checksum=checksum, person=person, vault_id=vault_id))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None
    landed = await land(session, source, waiting_room(config), waiting_seen(config),
                        named(name), checksum)
    await session.commit()
    source.unlink(missing_ok=True)
    return landed


async def claimed_by(session, checksum: bytes, photo_id: int) -> str | None:
    """Who offered this, if anyone did. Read once, as the picture enters the
    catalogue, and then the note has done its job — but not before it has told
    the vault which row in the library its own file became."""
    await _one_at_a_time(session, checksum)
    pending = await session.get(PhotoOffer, checksum)
    if pending is None:
        return None
    person, vault_id = pending.person, pending.vault_id
    await _link(session, vault_id, person, photo_id)
    await session.delete(pending)
    return person
