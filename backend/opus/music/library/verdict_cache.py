"""Deep-pass verdict cache: an unchanged folder judged against an unchanged
catalog reaches the same verdict, so replay it instead of paying the
rate-limited multi-source hunt again. The fingerprint covers everything whose
change could change that verdict — including the matcher itself, via
MATCHER_FINGERPRINT_VERSION."""

import hashlib

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from opus.db import SessionLocal
from opus.models import RELEASE_SOURCE_IDS, FolderScanCache, MusicFile, Release

# bump when matching logic changes — cached deep-pass verdicts from the old
# matcher must not suppress a re-run that could now link more
MATCHER_FINGERPRINT_VERSION = "1"


async def folder_fingerprint(session, artist_id: int, paths: list[str]) -> str:
    """Everything whose change could change the deep-pass verdict: the
    folder's files (path/size/mtime), the artist's release list (ids, titles,
    per-source ids, track counts) and the matcher version."""
    file_rows = (await session.execute(
        select(MusicFile.path, MusicFile.size, MusicFile.mtime)
        .where(MusicFile.path.in_(paths)).order_by(MusicFile.path)
    )).all()
    release_rows = (await session.execute(
        select(Release.id, Release.title,
               *(getattr(Release, column) for column in RELEASE_SOURCE_IDS),
               Release.track_count, Release.status)
        .where(Release.artist_id == artist_id).order_by(Release.id)
    )).all()
    payload = repr((MATCHER_FINGERPRINT_VERSION,
                    [tuple(r) for r in file_rows],
                    [tuple(r) for r in release_rows]))
    return hashlib.sha256(payload.encode()).hexdigest()


async def cached_verdict(session, rel: str, fingerprint: str) -> FolderScanCache | None:
    cached = await session.get(FolderScanCache, rel)
    if cached is not None and cached.fingerprint == fingerprint:
        return cached
    return None


async def store_verdict(rel: str, fingerprint: str, entry: dict) -> None:
    async with SessionLocal() as session:
        await session.execute(
            insert(FolderScanCache)
            .values(folder=rel, fingerprint=fingerprint,
                    outcome=entry["outcome"], artist_id=entry.get("artist_id"),
                    artist_name=entry.get("artist"), album=entry.get("album"),
                    matched=entry.get("matched"), total=entry.get("total"))
            .on_conflict_do_update(index_elements=["folder"], set_={
                "fingerprint": fingerprint,
                "outcome": entry["outcome"],
                "artist_id": entry.get("artist_id"),
                "artist_name": entry.get("artist"),
                "album": entry.get("album"),
                "matched": entry.get("matched"),
                "total": entry.get("total"),
                "checked_at": func.now(),
            })
        )
        await session.commit()
