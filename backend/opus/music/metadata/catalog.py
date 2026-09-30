"""The music catalogue: one metadata surface over several backends.

Deezer's open, auth-free API is the backbone and the one built in. A plugin
(opus.plugins) may add catalogues trusted above it, asked first and in their
order; a search one of them cannot answer — it is down, or it simply does not
know a usually very obscure artist — falls through to the next, and to Deezer
last.

Every backend returns the SAME dict shapes as DeezerClient so the scan/enrich
matcher is source-agnostic, and every dict carries a `source` key naming the
catalogue whose id namespace it holds: two catalogues' ids do not interchange,
so `get_*` is routed by source, never guessed, and a source's ids are kept in
its own `<source>_id` columns.
"""

import logging
from typing import Protocol

from opus_core import plugins

from opus.music.metadata.deezer import DeezerClient
from opus.plugins import CATALOGS

log = logging.getLogger("opus.catalog")

DEEZER = "deezer"
# the catalogues trusted above Deezer, in the order they are asked
PRIMARY: tuple[str, ...] = tuple(source for source, _ in CATALOGS)
SOURCES: tuple[str, ...] = (*PRIMARY, DEEZER)


class Catalog(Protocol):
    """A catalogue a plugin adds. Beside the DeezerClient shape it answers an
    album's cover and an artist's portrait as artwork candidates, says where its
    pictures stand among equals, and names an album's spatial mix in the album's
    `mixes` (opus.music.metadata.variants)."""

    source: str
    artwork_rank: int

    async def search_artists(self, query: str) -> list[dict]: ...
    async def search_albums(self, query: str) -> list[dict]: ...
    async def search_tracks(self, query: str) -> list[dict]: ...
    async def get_artist(self, catalog_id: int) -> dict: ...
    async def get_artist_albums(self, catalog_id: int) -> list[dict]: ...
    async def get_album(self, catalog_id: int) -> dict: ...
    async def get_album_tracks(self, catalog_id: int) -> list[dict]: ...
    async def cover(self, catalog_id: int) -> list[dict]: ...
    async def portrait(self, catalog_id: int) -> list[dict]: ...
    async def close(self) -> None: ...


def id_field(source: str | None) -> str:
    """The column a source's ids are kept in."""
    return f"{source or DEEZER}_id"


def held_by(row) -> tuple[str, int | None]:
    """The most trusted catalogue that knows this artist or release, and its id
    there; Deezer and no id when none does."""
    for source in SOURCES:
        if (catalog_id := getattr(row, id_field(source))) is not None:
            return source, catalog_id
    return DEEZER, None


def _kind(source: str) -> type[Catalog]:
    return plugins.resolve(dict(CATALOGS)[source])


def open_catalog(source: str) -> Catalog:
    return _kind(source)()


def artwork_rank(source: str) -> int:
    return _kind(source).artwork_rank


class CatalogClient:
    """The plugins' catalogues first, Deezer last. Search methods fall through
    on empty/error; every result dict is source-tagged. The id-keyed `get_*`
    methods are routed by that source."""

    def __init__(self):
        self.primary: dict[str, Catalog] = {source: open_catalog(source) for source in PRIMARY}
        self.deezer = DeezerClient()

    async def close(self):
        for catalog in self.primary.values():
            await catalog.close()
        await self.deezer.close()

    async def _search(self, method: str, query: str) -> list[dict]:
        for source, catalog in self.primary.items():
            try:
                hits = await getattr(catalog, method)(query)
                if hits:
                    return hits
            except Exception as exc:
                log.warning("%s %s failed, falling through: %s", source, method, exc)
        rows = await getattr(self.deezer, method)(query)
        for r in rows:
            r["source"] = DEEZER
        return rows

    async def search_artists(self, query: str) -> list[dict]:
        return await self._search("search_artists", query)

    async def search_albums(self, query: str) -> list[dict]:
        return await self._search("search_albums", query)

    async def search_tracks(self, query: str) -> list[dict]:
        return await self._search("search_tracks", query)

    async def get_artist_albums(self, catalog_id: int, source: str) -> list[dict]:
        if source in self.primary:
            return await self.primary[source].get_artist_albums(catalog_id)
        rows = await self.deezer.get_artist_albums(catalog_id)
        for r in rows:
            r["source"] = DEEZER
        return rows

    async def get_album(self, catalog_id: int, source: str) -> dict:
        if source in self.primary:
            return await self.primary[source].get_album(catalog_id)
        d = await self.deezer.get_album(catalog_id)
        d["source"] = DEEZER
        return d

    async def get_album_tracks(self, catalog_id: int, source: str) -> list[dict]:
        if source in self.primary:
            return await self.primary[source].get_album_tracks(catalog_id)
        rows = await self.deezer.get_album_tracks(catalog_id)
        for r in rows:
            r["source"] = DEEZER
        return rows
