import asyncio
import ctypes
import errno
import logging
import os
import re
from pathlib import Path

from sqlalchemy import func, select, update

from opus.db import SessionLocal
from opus.models import Setting, VaultFile
from opus.photos.vault import store
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)
VERSION_KEY = "vault_storage_identity_version"
VERSION = "1"
_IDENTITY = re.compile(r"[0-9a-f]{32}")
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_libc = ctypes.CDLL(None, use_errno=True)
_rename = _libc.renameat2
_rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
_rename.restype = ctypes.c_int


def _sync(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _same_prefix(source: Path, target: Path) -> bool:
    with source.open("rb") as left, target.open("rb") as right:
        remaining = min(source.stat().st_size, target.stat().st_size)
        while remaining:
            size = min(remaining, 1024 * 1024)
            if left.read(size) != right.read(size):
                return False
            remaining -= size
    return True


def _move(source: Path, target: Path) -> None:
    if target.parent.is_symlink():
        raise RuntimeError(f"vault storage contains a linked directory: {target.parent}")
    target.parent.mkdir(exist_ok=True)
    _sync(target.parent.parent)
    if _rename(_AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target), _RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        if error != errno.EEXIST:
            raise OSError(error, os.strerror(error), str(source))
        if target.is_symlink() or not target.is_file() or not _same_prefix(source, target):
            raise RuntimeError(f"conflicting vault objects: {source} and {target}")
        if source.stat().st_size > target.stat().st_size:
            os.replace(source, target)
        else:
            source.unlink()
    _sync(target.parent)
    _sync(source.parent)


def relocate(root: Path, owners: dict[str, str]) -> int:
    if not root.is_dir():
        raise RuntimeError(f"vault storage is not mounted: {root}")
    moved = 0
    for directory in sorted(root.iterdir()):
        if directory.is_symlink():
            raise RuntimeError(f"vault storage contains a linked directory: {directory}")
        if not directory.is_dir():
            continue
        for source in sorted(directory.iterdir()):
            file_id = source.name.removesuffix(".t")
            owner = owners.get(file_id)
            if owner is None or not _IDENTITY.fullmatch(owner):
                continue
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"vault object is not a regular file: {source}")
            target = root / owner / source.name
            if source == target:
                continue
            _move(source, target)
            moved += 1
        if not _IDENTITY.fullmatch(directory.name) and not any(directory.iterdir()):
            directory.rmdir()
            _sync(root)
    return moved


async def migrate(config, sessions=SessionLocal) -> int:
    async with sessions() as session:
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext("opus:vault-storage"))))
        version = await session.get(Setting, VERSION_KEY)
        if version is not None and version.value == VERSION:
            return 0
        rows = (await session.execute(select(VaultFile.id, VaultFile.person, VaultFile.size, VaultFile.at))).all()
        owners = {file_id: owner for file_id, owner, size, at in rows}
        moved = await asyncio.to_thread(relocate, store.root(config), owners)
        for file_id, owner, expected, previous in rows:
            if not _IDENTITY.fullmatch(owner):
                continue
            size = store.held(config, owner, file_id)
            if size == 0 and previous > 0:
                raise RuntimeError(f"vault object is missing from storage: {file_id}")
            if size > expected:
                raise RuntimeError(f"vault object exceeds its registered size: {file_id}")
            await session.execute(update(VaultFile).where(VaultFile.id == file_id).values(at=size))
        if version is None:
            session.add(Setting(key=VERSION_KEY, value=VERSION))
        else:
            version.value = VERSION
        await session.commit()
        log.info("vault storage identity migration complete: %d objects relocated", moved)
        return moved


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await migrate(await current_runtime())


if __name__ == "__main__":
    asyncio.run(main())
