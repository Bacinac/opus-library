"""Library scan: inventory the music_dir tree and adopt it in place. Owns the
three-phase walk (inventory → artist resolution → match), the progress state
the UI polls and the per-folder report; the verdict on any single folder is
foldermatch's.

Every audio file becomes (or updates) a `files` row FIRST — the file's
Postgres id is its identity; tags, name and quality are attributes. Matching
a file to a catalog track is a nullable link decided afterwards, tag-title
first (exact normalized, then fuzzy) with the filename as fallback. Files
are never moved or retagged by the scan.

Artists persist the moment they are created — a folder that later fails to
match an album must NOT roll the artist back: the post-scan enricher
(Discogs/Spotify/Wikidata) is what repairs regional catalog gaps, and it can
only work on artists that exist."""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import delete, select

from opus.db import SessionLocal
from opus.passes import Pass
from opus.music.library import (artists, foldermatch, inventory, sweeps,
                           verdict_cache)
from opus.music.metadata import catalog as catalogs
from opus.music.metadata.catalog import CatalogClient
from opus.music.metadata.discogs import DiscogsClient
from opus.music.metadata.spotify import SpotifyClient
from opus.models import Artist, FolderScanCache, MusicFile
from opus.settings_store import current_runtime
from opus.music.tagging.tagger import AUDIO_EXTENSIONS

log = logging.getLogger("opus.libimport")

job = Pass(
    "library scan", timed=False,
    # A scan is three passes over the library — read the files, resolve the
    # artists, match the folders — and the counter belongs to the pass now
    # running. One counter spanning all three sat at zero through the longest
    # of them, which reads as a scan that has stalled.
    phase=None, total=0, processed=0, matched=0, adopted_tracks=0, current=None,
    # deep lane: folders whose match escalated past the existing catalog into
    # the full multi-source machinery (edition sweep, evidence chain)
    deep_total=0, deep_processed=0, deep_current=None,
    # one entry per processed folder, in scan order:
    # {folder, outcome: adopted|partial|no_tags|artist_not_found|
    #  album_not_found|catalog_unavailable|error, artist?, album?,
    #  matched?, total?, detail?}
    results=[],
)


def start(chain_enrich: bool = True) -> bool:
    if job.running:
        return False
    inventory.seen_paths.clear()
    artists.created_artist_ids.clear()
    return job.start(_scan, chain_enrich)


async def rematch_folder(rel: str) -> dict:
    """Single-folder rerun of the scan pipeline (inventory → artist → match)
    — the quick action behind the unmatched-folders list. Appends its outcome
    to the scan report and returns it. The caller guards against a running
    scan."""
    config = await current_runtime()
    root = Path(config.get("music_dir")).resolve()
    directory = (root / rel).resolve()
    if root not in directory.parents or not directory.is_dir():
        raise FileNotFoundError(f"no such library folder: {rel}")
    files = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )
    if not files:
        raise FileNotFoundError(f"no audio files in {rel}")

    # a manual retry is a deliberate re-run — the cached verdict must neither
    # short-circuit it nor survive it stale
    async with SessionLocal() as session:
        await session.execute(delete(FolderScanCache).where(
            FolderScanCache.folder == str(directory.relative_to(root))))
        await session.commit()

    marker = len(job.state["results"])
    tag_artist, tag_album = await inventory.inventory_folder(files)
    if not (tag_artist and tag_album):
        record(directory, root, "no_tags", reason="missing_tags")
    else:
        catalog, discogs, spotify = _clients(config)
        try:
            async with SessionLocal() as session:
                songs = [
                    row[0] for row in (await session.execute(
                        select(MusicFile.tag_title).where(
                            MusicFile.path.in_([str(p) for p in files]),
                            MusicFile.tag_title.is_not(None),
                        )
                    )).all()
                ]
                artist = await artists.resolve_artist(session, catalog, discogs,
                                                      tag_artist, tag_album, songs)
                artist_id = artist.id if artist is not None else None
                if artist is not None:
                    await session.commit()
            if artist_id is None:
                record(directory, root, "artist_not_found",
                       artist=tag_artist, album=tag_album, reason="artist_unresolved")
            else:
                progress = {"escalated": False}
                if not await foldermatch.fast_gate_folder(artist_id, tag_album,
                                                          files, directory, root):
                    await foldermatch.match_folder(catalog, discogs, spotify,
                                                   artist_id, tag_artist,
                                                   tag_album, directory, files,
                                                   root, progress)
        finally:
            await _close(catalog, discogs, spotify)

    fresh = [r for r in job.state["results"][marker:]
             if r["folder"] == str(directory.relative_to(root))]
    return fresh[-1] if fresh else {"folder": rel, "outcome": "error"}


def _clients(config) -> tuple[CatalogClient, DiscogsClient | None, SpotifyClient | None]:
    catalog = CatalogClient()
    discogs = (DiscogsClient(config.get("discogs_token"))
               if config.get("discogs_token") else None)
    spotify = (SpotifyClient(config.get("spotify_client_id"),
                             config.get("spotify_client_secret"))
               if config.get("spotify_client_id") and config.get("spotify_client_secret")
               else None)
    return catalog, discogs, spotify


async def _close(catalog, discogs, spotify) -> None:
    await catalog.close()
    if discogs is not None:
        await discogs.close()
    if spotify is not None:
        await spotify.close()


async def _scan(chain_enrich: bool):
    state = job.state
    completed = False
    try:
        config = await current_runtime()
        root = Path(config.get("music_dir"))

        album_dirs = await asyncio.to_thread(inventory.collect_album_dirs, root)
        log.info("library scan: %d album folders under %s", len(album_dirs), root)
        if not album_dirs:
            # an unmounted library reads as an empty tree, and the sweeps would
            # take that literally: every file row deleted, every COMPLETE
            # release demoted. Nothing to scan is never a reason to forget.
            log.error("library scan: %s holds no audio — refusing to sweep", root)
            return

        catalog, discogs, spotify = _clients(config)
        try:
            folders = await _read(root, album_dirs)
            resolved = await _artists(catalog, discogs, folders)
            await _match(_Matching(root, catalog, discogs, spotify, resolved), folders)
        finally:
            await _close(catalog, discogs, spotify)
        completed = True
        await sweeps.sweep_missing_files()
        await sweeps.sweep_status_truth()
        await sweeps.sweep_orphans()
        log.info("library scan finished: %d/%d folders matched",
                 state["matched"], state["total"])
        _enrich_what_was_found(chain_enrich)
    finally:
        state["phase"] = None
        if not completed:
            inventory.seen_paths.clear()


async def _read(root: Path, album_dirs: dict[Path, list[Path]]
                ) -> list[tuple[Path, list[Path], str, str]]:
    """PHASE 1 — inventory: probe every folder (local disk), upsert file rows,
    collect the tags. A missing artist here does NOT drag the folder into deep —
    resolution is a separate phase."""
    state = job.state
    folders: list[tuple[Path, list[Path], str, str]] = []
    state.update(phase="read", total=len(album_dirs), processed=0)
    for directory, files in album_dirs.items():
        state["current"] = str(directory.relative_to(root))
        try:
            tag_artist, tag_album = await inventory.inventory_folder(files)
        except Exception:
            log.exception("inventory failed on %s", directory)
            record(directory, root, "error", reason="files_unreadable")
        else:
            if tag_artist and tag_album:
                folders.append((directory, files, tag_artist, tag_album))
            else:
                record(directory, root, "no_tags", reason="missing_tags")
        state["processed"] += 1
    state["current"] = None
    return folders


async def _artists(catalog, discogs, folders: list[tuple[Path, list[Path], str, str]]
                   ) -> dict[str, int]:
    """PHASE 2 — artists: resolve every referenced tag name to an artist_id ONCE,
    and hand phase 3 that map. Resolution can adopt an existing artist by service
    ID even when the tag name does not fuzzy-match the canonical name ("Bare i
    plaćenici" -> "GORAN BARE & PLAĆENICI"), which phase 3's name-only lookup
    could not do. Serial by design — two tag spellings can resolve to the same
    unique-id artist and would race; the Discogs 60/min throttle bounds this
    phase regardless."""
    state = job.state
    name_album: dict[str, str] = {}
    for _, _, tag_artist, tag_album in folders:
        name_album.setdefault(tag_artist, tag_album)
    resolved, missing, to_backfill, name_songs = await _artists_held(name_album)
    # lifting existing artists onto the catalogues above Deezer and resolving
    # new ones are both network-bound; they share the one progress line and commit per
    # artist so the id/name lands visibly, one at a time
    state.update(phase="artists", processed=0,
                 total=len(to_backfill) + len(missing))
    for artist_id, name in to_backfill:
        state["current"] = name
        try:
            async with SessionLocal() as session:
                artist = await session.get(Artist, artist_id)
                await artists.adopt_catalog_ids(session, catalog, artist)
                await session.commit()
        except Exception:
            log.exception("catalogue id adoption failed for %s", name)
        state["processed"] += 1
    for name, album in missing:
        state["current"] = name
        try:
            async with SessionLocal() as session:
                artist = await artists.resolve_artist(
                    session, catalog, discogs, name, album,
                    name_songs.get(name, []))
                if artist is not None:
                    resolved[name] = artist.id
                    await session.commit()
        except Exception:
            log.exception("artist resolution failed for %s", name)
        state["processed"] += 1
    state["current"] = None
    return resolved


async def _artists_held(name_album: dict[str, str]) -> tuple[
        dict[str, int], list[tuple[str, str]], list[tuple[int, str]], dict[str, list[str]]]:
    """The tag names the catalogue already answers for, the ones it does not, the
    known artists some catalogue above Deezer does not hold yet, and a sample of each name's songs."""
    resolved: dict[str, int] = {}
    missing: list[tuple[str, str]] = []
    to_backfill: list[tuple[int, str]] = []
    name_songs: dict[str, list[str]] = {}
    async with SessionLocal() as session:
        # a sample of each name's actual song titles — homonym resolution
        # is grounded in these, not just a (often generic) album title
        for tag_artist, title in (await session.execute(
            select(MusicFile.tag_artist, MusicFile.tag_title).where(
                MusicFile.tag_artist.in_(list(name_album.keys())),
                MusicFile.tag_title.isnot(None),
            )
        )).all():
            bucket = name_songs.setdefault(tag_artist, [])
            if len(bucket) < 20:
                bucket.append(title)
        for name, album in name_album.items():
            artist = await artists.find_artist_db(session, name, album)
            if artist is not None:
                resolved[name] = artist.id
                if any(getattr(artist, catalogs.id_field(source)) is None
                       for source in catalogs.PRIMARY):
                    to_backfill.append((artist.id, name))
            else:
                missing.append((name, album))
        await session.commit()
    return resolved, missing, to_backfill, name_songs


@dataclass
class _Matching:
    root: Path
    catalog: CatalogClient
    discogs: DiscogsClient | None
    spotify: SpotifyClient | None
    resolved: dict[str, int]
    locks: dict[int, asyncio.Lock] = field(default_factory=dict)


async def _match(matching: _Matching, folders: list[tuple[Path, list[Path], str, str]]) -> None:
    """PHASE 3 — match: every folder against its (now-existing) artist. The fast
    gate settles still-green folders from the DB alone; only a folder that is not
    a 100% match escalates into the deep machinery (counted via deep_* inside
    match_folder). A per-artist lock serializes same-artist folders against
    release-row races."""
    state = job.state
    state.update(phase="match", total=len(folders), processed=0)
    queue: asyncio.Queue = asyncio.Queue()
    for folder in folders:
        queue.put_nowait(folder)
    # deep folders sit on throttled APIs for minutes — enough workers
    # that green (DB-only) folders keep flowing past them
    await asyncio.gather(*(_match_worker(matching, queue) for _ in range(6)))
    state["current"] = None
    state["deep_current"] = None


async def _match_worker(matching: _Matching, queue: asyncio.Queue) -> None:
    state = job.state
    while True:
        try:
            directory, files, tag_artist, tag_album = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        state["current"] = str(directory.relative_to(matching.root))
        artist_id = matching.resolved.get(tag_artist)
        if artist_id is None:
            record(directory, matching.root, "artist_not_found",
                   artist=tag_artist, album=tag_album, reason="artist_unresolved")
            state["processed"] += 1
            continue
        # serialize same-artist folders (release-row races), keyed
        # on the resolved id so aliases sharing an artist also lock
        async with matching.locks.setdefault(artist_id, asyncio.Lock()):
            await _match_folder(matching, artist_id, directory, files, tag_artist, tag_album)
        state["processed"] += 1


async def _match_folder(matching: _Matching, artist_id: int, directory: Path,
                        files: list[Path], tag_artist: str, tag_album: str) -> None:
    state = job.state
    root = matching.root
    progress = {"escalated": False}
    try:
        if not await foldermatch.fast_gate_folder(
                artist_id, tag_album, files, directory, root):
            await _match_deep(matching, artist_id, directory, files, tag_artist,
                              tag_album, progress)
    except httpx.HTTPError as exc:
        # A transient upstream network fault (read timeout, reset) must not
        # look like an album that does not exist. The report distinguishes an
        # actionable retry from a genuine catalogue miss without exposing an
        # upstream URL or response body to the browser.
        log.warning("network fault matching %s: %s — retrying next scan",
                    directory, exc)
        record(directory, root, "catalog_unavailable", artist=tag_artist,
               album=tag_album, detail=type(exc).__name__, reason="catalog_unreachable")
    except Exception as exc:
        log.exception("match failed on %s", directory)
        record(directory, root, "error", artist=tag_artist, album=tag_album,
               detail=type(exc).__name__, reason="match_error")
    finally:
        if progress["escalated"]:
            state["deep_processed"] += 1


async def _match_deep(matching: _Matching, artist_id: int, directory: Path,
                      files: list[Path], tag_artist: str, tag_album: str,
                      progress: dict) -> None:
    """An unchanged folder against an unchanged catalog reaches the same verdict
    — the rate-limited deep hunt is skipped and its verdict replayed."""
    state = job.state
    root = matching.root
    rel = str(directory.relative_to(root))
    async with SessionLocal() as session:
        fingerprint = await verdict_cache.folder_fingerprint(
            session, artist_id, [str(p) for p in files])
        cached = await verdict_cache.cached_verdict(
            session, rel, fingerprint)
    if cached is not None:
        record(directory, root, cached.outcome,
               artist=cached.artist_name or tag_artist,
               artist_id=cached.artist_id or artist_id,
               album=cached.album or tag_album,
               matched=cached.matched or 0,
               total=cached.total or 0, reason="cached_verdict")
        if cached.outcome == "adopted":
            state["matched"] += 1
            state["adopted_tracks"] += cached.matched or 0
        return
    marker = len(state["results"])
    await foldermatch.match_folder(
        matching.catalog, matching.discogs, matching.spotify, artist_id,
        tag_artist, tag_album, directory, files,
        root, progress)
    entry = next(
        (r for r in reversed(state["results"][marker:])
         if r["folder"] == rel), None)
    if entry and entry["outcome"] != "error":
        await verdict_cache.store_verdict(
            rel, fingerprint, entry)


def _enrich_what_was_found(chain_enrich: bool) -> None:
    """Scan-created artists carry no metadata yet — enrich them; on the no-chain
    follow-up scan, enrich ONLY the artists this scan created (the set shrinks to
    empty, so the cycle converges instead of leaving fresh artists unrecognized
    forever)."""
    from opus.music.library import enrich_all
    if chain_enrich:
        enrich_all.start(rescan_after=True)
    elif artists.created_artist_ids:
        log.info("follow-up enrich for %d artists created by this scan",
                 len(artists.created_artist_ids))
        enrich_all.start(rescan_after=True,
                         artist_ids=sorted(artists.created_artist_ids))


def record(directory: Path, root: Path, outcome: str, **extra):
    job.state["results"].append(
        {"folder": str(directory.relative_to(root)), "outcome": outcome, **extra}
    )
