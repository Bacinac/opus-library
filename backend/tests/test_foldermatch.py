import logging
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import Artist, MusicFile, Release, ReleaseStatus, Track
from opus.music.library import albumsearch, editions, foldermatch, scan

ROOT = Path("/music")


async def _seed(*albums):
    """(artist_id, release_id, title, songs, status) per album."""
    async with db.SessionLocal() as session:
        for artist_id in sorted({a[0] for a in albums}):
            session.add(Artist(id=artist_id, name=f"Artist {artist_id}"))
        await session.flush()
        for artist_id, release_id, title, songs, status in albums:
            session.add(Release(id=release_id, artist_id=artist_id, title=title,
                                status=status, track_count=len(songs)))
            await session.flush()
            for i, song in enumerate(songs, 1):
                session.add(Track(id=release_id * 100 + i, release_id=release_id,
                                  position=i, title=song))
        await session.commit()


async def _folder(name: str, songs: list[str]) -> list[Path]:
    files = [ROOT / name / f"{i:02d} {song}.flac" for i, song in enumerate(songs, 1)]
    async with db.SessionLocal() as session:
        for i, (path, song) in enumerate(zip(files, songs), 1):
            session.add(MusicFile(path=str(path), tag_title=song, tag_track=i))
        await session.commit()
    return files


def _match(artist_id: int, album: str, files: list[Path]) -> dict:
    progress: dict = {}
    run(foldermatch.match_folder(None, None, None, artist_id, f"Artist {artist_id}", album,
                                 files[0].parent, files, ROOT, progress))
    return progress


async def _linked(files: list[Path]) -> list[int | None]:
    async with db.SessionLocal() as session:
        by_path = {f.path: f.track_id for f in (await session.execute(select(MusicFile))).scalars()}
    return [by_path[str(p)] for p in files]


async def _release(release_id: int):
    async with db.SessionLocal() as session:
        release = await session.get(Release, release_id)
        tracks = (await session.execute(
            select(Track.id).where(Track.release_id == release_id))).scalars().all()
        return release.status, release.track_count, sorted(tracks)


def _outcome() -> dict:
    return {k: v for k, v in scan.job.state["results"][-1].items() if k != "artist"}


SONGS = ["Balkan", "Pavel", "Hladan kao led", "Poljubi me"]


def test_a_folder_is_matched_by_what_it_holds(clean, monkeypatch, caplog):
    scan.job.state.update(scan.job._reset())
    fallbacks = []

    async def no_ids(session, artist):
        return {}

    async def fallback(session, catalog, discogs, spotify, artist, external, *rest):
        fallbacks.append(artist.id)
        return await session.get(Release, 50) if artist.id == 4 else None

    async def confirmed(session, discogs, release, *rest):
        return SimpleNamespace(evidence="discogs") if release.id == 60 else None

    monkeypatch.setattr(albumsearch, "resolve_external_ids", no_ids)
    monkeypatch.setattr(albumsearch, "album_search_fallback", fallback)
    monkeypatch.setattr(editions, "confirm_edition", confirmed)
    NONE, COMPLETE = ReleaseStatus.NONE, ReleaseStatus.COMPLETE
    run(_seed(
        (1, 10, "Sunčana Strana Ulice (Deluxe)", SONGS + ["Kurvini sinovi"], NONE),
        (1, 11, "Sunčana Strana Ulice", SONGS, NONE),
        (2, 20, "Filigranski Pločnici", SONGS, COMPLETE),
        (3, 30, "Kad Fazani Lete Box", [f"Song {i}" for i in range(25)], NONE),
        (5, 50, "Ravno Do Dna", SONGS[:2], NONE),
        (6, 60, "Krivo Srastanje", SONGS, NONE),
    ))

    # the complete standard edition beats the deluxe that would leave a gap
    full = run(_folder("Azra/Suncana", SONGS))
    assert _match(1, "Sunčana Strana Ulice", full) == {}
    assert run(_linked(full)) == [1101, 1102, 1103, 1104]
    assert run(_release(11))[0] == COMPLETE
    assert _outcome() == {"folder": "Azra/Suncana", "outcome": "adopted", "artist_id": 1,
                          "album": "Sunčana Strana Ulice", "matched": 4, "total": 4,
                          "reason": "all_tracks_matched"}

    # half an album escalates, finds nothing better and stops being complete
    half = run(_folder("Azra/Filigranski", SONGS[:2]))
    assert _match(2, "Filigranski Pločnici", half) == {"escalated": True}
    assert run(_linked(half)) == [2001, 2002]
    assert run(_release(20))[0] == NONE
    assert _outcome()["outcome"] == "partial"
    assert _outcome()["reason"] == "partial_track_match"
    assert scan.job.state["deep_current"] == "Azra/Filigranski"

    # a box set does not swallow a small folder, and nothing else is found
    small = run(_folder("Azra/Fazani", [f"Song {i}" for i in range(5)]))
    with caplog.at_level(logging.INFO, logger="opus.libimport"):
        assert _match(3, "Kad Fazani Lete", small) == {"escalated": True}
    assert "box-set guard" in caplog.text
    assert run(_linked(small)) == [None] * 5
    assert _outcome() == {"folder": "Azra/Fazani", "outcome": "album_not_found",
                          "artist_id": 3, "album": "Kad Fazani Lete",
                          "reason": "no_catalog_candidate"}

    # what the other sources find is matched like any edition
    found = run(_folder("Azra/Ravno", SONGS[:2]))
    run(_seed((4, 40, "Nešto Sasvim Drugo", ["Other"], NONE)))
    assert _match(4, "Ravno Do Dna", found) == {"escalated": True}
    assert run(_linked(found)) == [5001, 5002]
    assert _outcome()["outcome"] == "adopted"

    # every file of a real edition is complete even where the catalogue row
    # carries a track more — and the extra row goes
    edition = run(_folder("Azra/Krivo", SONGS[:3]))
    assert _match(6, "Krivo Srastanje", edition) == {"escalated": True}
    assert run(_release(60)) == (COMPLETE, 3, [6001, 6002, 6003])
    assert _outcome()["reason"] == "edition_confirmed"
    assert (_outcome()["matched"], _outcome()["total"]) == (3, 3)

    assert fallbacks == [2, 3, 4, 6]
    assert (scan.job.state["matched"], scan.job.state["deep_total"],
            scan.job.state["adopted_tracks"]) == (3, 4, 11)
