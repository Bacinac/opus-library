from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.dialects.postgresql import insert

from conftest import run
from opus import db
from opus.models import Artist, Setting
from opus.music.library import artists, enrich_all, foldermatch, inventory, scan, sweeps, verdict_cache
from opus.music.metadata import catalog as catalogs

# folder -> (tags, artist found in the catalogue, the fast gate settles it, what a deep match does)
FOLDERS = {
    "Known/Settled": (("Known", "Settled"), "known", True, None),
    "Untagged/Album": ((None, None), None, None, None),
    "Broken/Album": ("raise", None, None, None),
    "Fresh/Adopted": (("Fresh", "Adopted"), "resolved", False, "partial"),
    "Nobody/Album": (("Nobody", "Album"), None, None, None),
    "Unheld/Settled": (("Unheld", "Settled"), "known-unheld", True, None),
    "Known/Cached": (("Known", "Cached"), "known", False, "cached"),
    "Known/Deep": (("Known", "Deep"), "known", False, "adopted"),
    "Known/Timeout": (("Known", "Timeout"), "known", False, "timeout"),
    "Known/Crash": (("Known", "Crash"), "known", False, "crash"),
}


class Recorded:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *call):
        self.calls.append(call)

    def named(self, name: str) -> list[tuple]:
        return [c[1:] for c in self.calls if c[0] == name]


@pytest.fixture
def library_tree(tmp_path, monkeypatch, clean):
    root = tmp_path / "music"
    dirs = {root / name: [root / name / "01.flac", root / name / "02.flac"] for name in FOLDERS}
    said = Recorded()

    async def configure():
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(key="music_dir", value=str(root)))
            session.add_all([Artist(id=1, name="Known"), Artist(id=2, name="Unheld"),
                             Artist(id=3, name="Fresh")])
            await session.commit()

    run(configure())

    def folder(files: list[Path]) -> str:
        return str(files[0].parent.relative_to(root))

    async def inventory_folder(files):
        tags = FOLDERS[folder(files)][0]
        if tags == "raise":
            raise OSError("unreadable")
        return tags

    async def find_artist_db(session, name, album):
        return {"Known": SimpleNamespace(id=1, above_id=10),
                "Unheld": SimpleNamespace(id=2, above_id=None)}.get(name)

    async def adopt_catalog_ids(session, catalog, artist):
        said("adopt", artist.id)

    async def resolve_artist(session, catalog, discogs, name, album, songs):
        said("resolve", name, album)
        if name == "Fresh":
            artists.created_artist_ids.add(3)
            return SimpleNamespace(id=3)
        return None

    async def fast_gate_folder(artist_id, tag_album, files, directory, root_):
        return FOLDERS[folder(files)][2]

    async def folder_fingerprint(session, artist_id, paths):
        return f"print-{artist_id}"

    async def cached_verdict(session, rel, fingerprint):
        if FOLDERS[rel][3] != "cached":
            return None
        return SimpleNamespace(outcome="adopted", artist_name="Known", artist_id=1,
                               album="Cached", matched=7, total=7)

    async def store_verdict(rel, fingerprint, entry):
        said("stored", rel, fingerprint, entry["outcome"])

    async def match_folder(catalog, discogs, spotify, artist_id, tag_artist, tag_album,
                           directory, files, root_, progress):
        how = FOLDERS[folder(files)][3]
        progress["escalated"] = True
        if how == "timeout":
            raise httpx.ReadTimeout("slow")
        if how == "crash":
            raise RuntimeError("bug")
        scan.record(directory, root, how, matched=2, total=2)

    class Client:
        def __init__(self, *args):
            said("opened", type(self).__name__, args)

        async def close(self):
            said("closed", type(self).__name__)

    for name, fake in (("CatalogClient", type("CatalogClient", (Client,), {})),
                       ("DiscogsClient", type("DiscogsClient", (Client,), {})),
                       ("SpotifyClient", type("SpotifyClient", (Client,), {}))):
        monkeypatch.setattr(scan, name, fake)
    monkeypatch.setattr(inventory, "collect_album_dirs", lambda given: dirs if given == root else {})
    monkeypatch.setattr(inventory, "inventory_folder", inventory_folder)
    monkeypatch.setattr(artists, "find_artist_db", find_artist_db)
    monkeypatch.setattr(catalogs, "PRIMARY", ("above",))
    monkeypatch.setattr(artists, "adopt_catalog_ids", adopt_catalog_ids)
    monkeypatch.setattr(artists, "resolve_artist", resolve_artist)
    monkeypatch.setattr(foldermatch, "fast_gate_folder", fast_gate_folder)
    monkeypatch.setattr(foldermatch, "match_folder", match_folder)
    monkeypatch.setattr(verdict_cache, "folder_fingerprint", folder_fingerprint)
    monkeypatch.setattr(verdict_cache, "cached_verdict", cached_verdict)
    monkeypatch.setattr(verdict_cache, "store_verdict", store_verdict)
    for sweep in ("sweep_missing_files", "sweep_status_truth", "sweep_orphans"):
        async def swept(sweep=sweep):
            said("sweep", sweep)
        monkeypatch.setattr(sweeps, sweep, swept)
    monkeypatch.setattr(enrich_all, "start", lambda **kwargs: said("enrich", kwargs))
    return root, said


async def _scan(chain_enrich: bool) -> dict:
    assert scan.start(chain_enrich)
    return await scan.job.run(scan._scan, chain_enrich)


def test_a_scan_reads_resolves_and_matches(library_tree):
    root, said = library_tree
    state = run(_scan(True))
    outcomes = sorted((r["folder"], r["outcome"]) for r in state["results"])
    assert outcomes == [
        ("Broken/Album", "error"),
        ("Fresh/Adopted", "partial"),
        ("Known/Cached", "adopted"),
        ("Known/Crash", "error"),
        ("Known/Deep", "adopted"),
        ("Known/Timeout", "catalog_unavailable"),
        ("Nobody/Album", "artist_not_found"),
        ("Untagged/Album", "no_tags"),
    ]
    cached = next(r for r in state["results"] if r["folder"] == "Known/Cached")
    assert cached == {"folder": "Known/Cached", "outcome": "adopted", "artist": "Known",
                      "artist_id": 1, "album": "Cached", "matched": 7, "total": 7,
                      "reason": "cached_verdict"}
    timeout = next(r for r in state["results"] if r["folder"] == "Known/Timeout")
    assert timeout["detail"] == "ReadTimeout"
    assert (state["phase"], state["total"], state["processed"]) == (None, 8, 8)
    assert (state["matched"], state["adopted_tracks"]) == (1, 7)
    assert (state["deep_processed"], state["current"], state["deep_current"]) == (4, None, None)
    assert said.named("adopt") == [(2,)]
    assert said.named("resolve") == [("Fresh", "Adopted"), ("Nobody", "Album")]
    assert sorted(said.named("stored")) == [("Fresh/Adopted", "print-3", "partial"),
                                            ("Known/Deep", "print-1", "adopted")]
    assert [c[0] for c in said.named("closed")] == ["CatalogClient"]
    assert said.named("sweep") == [("sweep_missing_files",), ("sweep_status_truth",),
                                   ("sweep_orphans",)]
    assert said.named("enrich") == [({"rescan_after": True},)]


def test_a_follow_up_scan_enriches_only_what_it_created(library_tree):
    root, said = library_tree
    run(_scan(False))
    assert said.named("enrich") == [({"rescan_after": True, "artist_ids": [3]},)]


def test_the_other_catalogues_are_opened_when_configured(library_tree):
    root, said = library_tree

    async def configure():
        async with db.SessionLocal() as session:
            for key in ("discogs_token", "spotify_client_id", "spotify_client_secret"):
                await session.execute(insert(Setting).values(key=key, value=f"{key}-value"))
            await session.commit()

    run(configure())
    run(_scan(True))
    assert sorted(said.named("opened")) == [
        ("CatalogClient", ()), ("DiscogsClient", ("discogs_token-value",)),
        ("SpotifyClient", ("spotify_client_id-value", "spotify_client_secret-value"))]
    assert sorted(c[0] for c in said.named("closed")) == ["CatalogClient", "DiscogsClient",
                                                          "SpotifyClient"]


def test_an_empty_library_is_never_swept(library_tree, monkeypatch):
    root, said = library_tree
    monkeypatch.setattr(inventory, "collect_album_dirs", lambda given: {})
    inventory.seen_paths.add("/music/somewhere.flac")
    state = run(_scan(True))
    assert state["results"] == [] and state["phase"] is None
    assert said.named("sweep") == [] and said.named("enrich") == []
    assert inventory.seen_paths == set()
