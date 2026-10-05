import asyncio
import logging
import os
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opus.db import SessionLocal
from opus.models import FileImport

log = logging.getLogger(__name__)
STAGING = ".opus-imports"


async def run(function, *args):
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(path: Path) -> list[int] | None:
    if path.is_symlink():
        raise OSError(f"import path is a symbolic link: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise OSError(f"import path is not a regular file: {path}")
    stat = path.stat()
    return [stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _replace(source: Path, dest: Path) -> None:
    source.replace(dest)
    _sync(source.parent)
    _sync(dest.parent)


class Transaction:
    def __init__(self, root: Path):
        self.root = root.absolute()
        self.id = secrets.token_hex(16)
        self.entries = {}
        self.connection = None
        self.connection_engine = None
        self.published = False
        self.locked = set()

    async def hold(self, key: str) -> None:
        if self.connection is None:
            self.connection_engine = create_async_engine(SessionLocal.kw["bind"].url, poolclass=NullPool)
            self.connection = await self.connection_engine.connect()
        if key not in self.locked:
            await self.connection.execute(
                text("SELECT pg_advisory_lock(hashtext(:path))"), {"path": key})
            self.locked.add(key)

    def reserve(self, dest: Path) -> Path:
        dest = dest.absolute()
        dest.relative_to(self.root)
        if str(dest) in self.entries:
            raise ValueError(f"duplicate import destination: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        directory = self.root / STAGING / self.id / str(len(self.entries))
        directory.mkdir(parents=True)
        self.entries[str(dest)] = {"dest": str(dest), "new": str(directory / dest.name),
                                   "old": str(directory / "previous")}
        return directory / dest.name

    def copy(self, source: Path, dest: Path) -> Path:
        before = _identity(source)
        stage = self.reserve(dest)
        shutil.copy2(source, stage)
        if _identity(source) != before or stage.stat().st_size != before[1]:
            raise OSError(f"import source changed during copy: {source}")
        return stage

    def adopt(self, stage: Path, dest: Path) -> None:
        target = self.reserve(dest)
        stage.replace(target)

    def retire(self, dest: Path) -> None:
        if str(dest.absolute()) not in self.entries:
            self.reserve(dest)
            self.entries[str(dest.absolute())]["new"] = None

    async def publish(self) -> None:
        if self.published:
            return
        await self._lock()
        entries = list(self.entries.values())
        await run(self._prepare, entries)
        await self.connection.execute(insert(FileImport).values(
            id=self.id, entries=entries, committed=False))
        await self.connection.commit()
        self.published = True
        await run(self._publish, entries)

    async def _lock(self) -> None:
        for path in sorted(self.entries):
            await self.hold(path)

    def _prepare(self, entries) -> None:
        for entry in entries:
            entry["before"] = _identity(Path(entry["dest"]))
            entry["after"] = _identity(Path(entry["new"])) if entry["new"] else None
            if entry["new"]:
                if entry["after"] is None:
                    raise FileNotFoundError(entry["new"])
                _sync(Path(entry["new"]))
            _sync(Path(entry["old"]).parent)
            _sync(Path(entry["old"]).parent.parent)
            _sync(Path(entry["old"]).parent.parent.parent)
            for directory in Path(entry["dest"]).parents:
                _sync(directory)

    def _publish(self, entries) -> None:
        for entry in entries:
            dest = Path(entry["dest"])
            if entry["before"] is not None:
                _replace(dest, Path(entry["old"]))
            if entry["new"]:
                _replace(Path(entry["new"]), dest)

    def _restore(self, entries) -> None:
        for entry in reversed(entries):
            dest, old = Path(entry["dest"]), Path(entry["old"])
            if old.exists():
                if _identity(old) != entry["before"]:
                    raise OSError(f"previous import file changed: {old}")
                if _identity(dest) not in (None, entry["after"]):
                    raise OSError(f"uncommitted import destination changed: {dest}")
                _replace(old, dest)
            elif entry["before"] is not None:
                if _identity(dest) != entry["before"]:
                    raise OSError(f"previous import file missing: {dest}")
            elif entry["new"] and not Path(entry["new"]).exists() and dest.exists():
                if _identity(dest) != entry["after"]:
                    raise OSError(f"uncommitted import file changed: {dest}")
                dest.unlink()
                _sync(dest.parent)

    def _clean(self) -> None:
        directories = {Path(entry["old"]).parent.parent for entry in self.entries.values()}
        for directory in directories:
            if directory.exists():
                shutil.rmtree(directory)
                _sync(directory.parent)

    async def resolve(self) -> bool:
        try:
            if not self.published:
                await run(self._clean)
                return False
            row = (await self.connection.execute(
                select(FileImport.committed).where(FileImport.id == self.id))).first()
            if row is None:
                raise RuntimeError(f"import journal missing: {self.id}")
            committed = row[0]
            if not committed:
                await run(self._restore, list(self.entries.values()))
            else:
                await run(self._verify)
            await run(self._clean)
            await self.connection.execute(delete(FileImport).where(FileImport.id == self.id))
            await self.connection.commit()
            return committed
        finally:
            if self.connection is not None:
                try:
                    await self.connection.rollback()
                    await self.connection.execute(text("SELECT pg_advisory_unlock_all()"))
                finally:
                    await self.connection.close()
                self.connection = None
                await self.connection_engine.dispose()
                self.connection_engine = None

    def _verify(self) -> None:
        for entry in self.entries.values():
            if _identity(Path(entry["dest"])) != entry["after"]:
                raise OSError(f"committed import file changed or missing: {entry['dest']}")


def transaction(session, root: Path | None = None) -> Transaction:
    if "file_import" not in session.info:
        if root is None:
            raise ValueError("import storage root is required")
        session.info["file_import"] = Transaction(root)
    return session.info["file_import"]


@asynccontextmanager
async def managed(session, root: Path):
    files = transaction(session, root)
    try:
        yield files
    finally:
        if "file_import" in session.info:
            await rollback(session)


async def commit(session) -> None:
    files = session.info.get("file_import")
    try:
        if files is not None:
            await files.publish()
            await session.execute(update(FileImport).where(FileImport.id == files.id)
                                  .values(committed=True))
        await session.commit()
    except BaseException:
        await session.rollback()
        session.info.pop("file_import", None)
        if files is None or not await files.resolve():
            raise
        log.exception("catalogue commit acknowledgement failed after a committed import")
    else:
        session.info.pop("file_import", None)
        if files is not None:
            try:
                await files.resolve()
            except Exception:
                log.exception("committed import %s needs cleanup on restart", files.id)


async def rollback(session) -> None:
    await session.rollback()
    files = session.info.pop("file_import", None)
    if files is not None:
        await files.resolve()


async def recover() -> None:
    async with SessionLocal() as session:
        rows = (await session.execute(select(FileImport))).scalars().all()
        for row in rows:
            files = Transaction(Path(row.entries[0]["old"]).parents[3])
            files.id = row.id
            files.entries = {entry["dest"]: entry for entry in row.entries}
            files.published = True
            await files._lock()
            committed = await files.resolve()
            log.info("recovered import %s (%s)", row.id,
                     "committed" if committed else "rolled back")


async def startup() -> None:
    from opus.settings_store import current_runtime

    await recover()
    config = await current_runtime()
    for key in ("music_dir", "movies_dir", "tv_dir", "video_dir"):
        await run(_clean_orphans, Path(config.get(key)))


def _clean_orphans(root: Path) -> None:
    stage = root / STAGING
    if stage.is_symlink():
        raise OSError(f"import staging directory is a symbolic link: {stage}")
    if not stage.exists():
        return
    for orphan in stage.iterdir():
        if re.fullmatch(r"[a-f0-9]{32}", orphan.name):
            if orphan.is_symlink() or not orphan.is_dir():
                raise OSError(f"invalid import staging directory: {orphan}")
            shutil.rmtree(orphan)
            _sync(stage)


if __name__ == "__main__":
    asyncio.run(startup())
