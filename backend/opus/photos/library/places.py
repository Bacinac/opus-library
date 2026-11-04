"""Turning coordinates into the name of a place.

Eight thousand photographs carry a latitude and a longitude, which is not a
place: nobody looks for 43.06, 17.42, they look for Podgora. The name is worked
out here, once, from a list of the world's settlements — offline, in this
container, because a lookup service would mean sending the coordinates of a
private archive to somebody else's machine and depending on their quota to
search our own photographs.

It never touches a place somebody typed. A name a person put on a photograph is
the best fact available about where they were; this is the second best, and the
second best does not overwrite the first. What a person types is looked up in
the same list, so a place named by hand brings the town's country and
coordinates with it: a camera that wrote Albania into a winter on Kupres is
corrected, not merely relabelled.

The list is GeoNames' cities500 — every settlement over five hundred people,
about two hundred thousand of them, thirteen megabytes fetched once. Under that
size a village stops being in the list, and the honest answer for a photograph
taken there is the nearest place that is.
"""

import asyncio
import bisect
import dataclasses
import io
import logging
import math
import pathlib
import zipfile

import httpx
import numpy as np
from sqlalchemy import bindparam, text

from opus.db import SessionLocal
from opus.models import Photo
from opus.photos.library import derive
from opus.photos.library.search import fold
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

SOURCE = "https://download.geonames.org/export/dump/cities500.zip"
# the counties, states and federal units, which is what tells three Zatons apart
REGIONS = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

# How far the nearest settlement may be before a photograph is left unnamed. A
# picture taken at sea or from an aeroplane is nearest to somewhere, and naming
# it after a town fifty kilometres away would be worse than saying nothing.
REACH_KM = 40.0


@dataclasses.dataclass(frozen=True)
class Settlement:
    id: int
    name: str
    country: str
    region: str
    latitude: float
    longitude: float


@dataclasses.dataclass
class Gazetteer:
    ids: np.ndarray
    latitude: np.ndarray
    longitude: np.ndarray
    population: np.ndarray
    names: list[str]
    countries: list[str]
    region_of: np.ndarray
    regions: list[str]
    # every name folded and sorted, so what has been typed so far is one
    # bisection rather than a walk through two hundred thousand towns
    keys: list[str] = dataclasses.field(init=False)
    keyed: np.ndarray = dataclasses.field(init=False)
    by_id: np.ndarray = dataclasses.field(init=False)

    def __post_init__(self):
        folded = [fold(name) for name in self.names]
        self.keyed = np.array(sorted(range(len(folded)), key=folded.__getitem__), np.int64)
        self.keys = [folded[i] for i in self.keyed]
        self.by_id = np.argsort(self.ids)

    def _at(self, row: int) -> Settlement:
        return Settlement(int(self.ids[row]), self.names[row], self.countries[row],
                          self.regions[self.region_of[row]],
                          float(self.latitude[row]), float(self.longitude[row]))

    def find(self, typed: str, been: dict[str, int], limit: int = 8) -> list[Settlement]:
        """The settlements a name could mean: those called exactly that before
        those it only begins; within each, the countries the archive has been to
        most, and then the larger town \u2014 Zaton is three villages in Croatia
        before it is a bigger one in Siberia."""
        key = fold(typed)
        if not key:
            return []
        start = bisect.bisect_left(self.keys, key)
        exact = bisect.bisect_right(self.keys, key)
        end = bisect.bisect_left(self.keys, key + "\uffff", lo=exact)
        rows = self.keyed[start:end]
        partial = np.arange(len(rows)) >= exact - start
        visits = np.array([been.get(self.countries[row], 0) for row in rows], np.int64)
        ranked = rows[np.lexsort((-self.population[rows], -visits, partial))]
        return [self._at(int(row)) for row in ranked[:limit]]

    def at(self, settlement: int) -> Settlement | None:
        spot = int(np.searchsorted(self.ids, settlement, sorter=self.by_id))
        if spot >= len(self.ids) or self.ids[self.by_id[spot]] != settlement:
            return None
        return self._at(int(self.by_id[spot]))


_held: Gazetteer | None = None
_holding = asyncio.Lock()


async def gazetteer() -> Gazetteer:
    """The world's towns, read once per process and kept: naming a photograph
    asks it while somebody is typing."""
    global _held
    async with _holding:
        if _held is None:
            _held = await _settlements(derive.store_root(await current_runtime()))
    return _held


async def _settlements(root: pathlib.Path) -> Gazetteer:
    """Fetched once and kept beside the derivatives."""
    kept, regions = root / "places" / "cities500.txt", root / "places" / "admin1.txt"
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        if not kept.exists():
            resp = await client.get(SOURCE)
            resp.raise_for_status()
            await asyncio.to_thread(_unpack, resp.content, kept)
        if not regions.exists():
            resp = await client.get(REGIONS)
            resp.raise_for_status()
            await asyncio.to_thread(_keep, resp.content, regions)
    return await asyncio.to_thread(_read, kept, regions)


def _unpack(bundle: bytes, kept: pathlib.Path) -> None:
    kept.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(bundle)) as opened:
        kept.write_bytes(opened.read("cities500.txt"))


def _keep(body: bytes, kept: pathlib.Path) -> None:
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(body)


def _read(kept: pathlib.Path, regions: pathlib.Path) -> Gazetteer:
    region_names = {}
    with regions.open(encoding="utf-8") as lines:
        for line in lines:
            bits = line.rstrip("\n").split("\t")
            if len(bits) >= 2:
                region_names[bits[0]] = bits[1]
    known: dict[str, int] = {"": 0}
    ids, lats, lons, people, names, countries, region_of = [], [], [], [], [], [], []
    with kept.open(encoding="utf-8") as lines:
        for line in lines:
            bits = line.split("\t")
            if len(bits) < 15:
                continue
            # A district is not a place. GeoNames marks a section of a town PPLX,
            # and those sit closer to a photograph than the town does — which is
            # how an evening in Berlin came back as Mitte and Schöneberg, and a
            # week in Vienna as Hietzing. Everything else stays, so a village
            # keeps its own name instead of being swallowed by the city near it.
            if bits[7] == "PPLX":
                continue
            try:
                row = int(bits[0]), float(bits[4]), float(bits[5]), int(bits[14] or 0)
            except ValueError:
                continue
            ids.append(row[0])
            lats.append(row[1])
            lons.append(row[2])
            people.append(row[3])
            names.append(bits[1][:120])
            countries.append(bits[8][:2])
            region = region_names.get(f"{bits[8]}.{bits[10]}", "")
            region_of.append(known.setdefault(region, len(known)))
    return Gazetteer(np.array(ids, np.int64), np.array(lats, np.float64),
                     np.array(lons, np.float64), np.array(people, np.int64),
                     names, countries, np.array(region_of, np.int32), list(known))


async def run() -> dict:
    """Name every located photograph that has not been asked about. One too far
from anywhere is written down as asked, with no name."""
    counts = {"named": 0, "too_far": 0}
    towns = await gazetteer()
    lat, lon = np.radians(towns.latitude), np.radians(towns.longitude)
    sin_lat, cos_lat = np.sin(lat), np.cos(lat)
    async with SessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT p.id, p.latitude, p.longitude FROM photos p
            WHERE p.latitude IS NOT NULL AND p.place = '' AND p.place_source = ''
            ORDER BY p.id
        """))).all()

        found, far = [], []
        for row in rows:
            a, b = math.radians(row.latitude), math.radians(row.longitude)
            # great-circle distance to every settlement at once; two hundred
            # thousand of them is one vector operation and a millisecond
            cos_d = (math.sin(a) * sin_lat
                     + math.cos(a) * cos_lat * np.cos(lon - b))
            near = int(np.argmax(cos_d))
            km = 6371.0 * math.acos(min(1.0, max(-1.0, float(cos_d[near]))))
            if km > REACH_KM:
                far.append({"b_id": row.id, "b_place": "", "b_country": ""})
                continue
            found.append({"b_id": row.id, "b_place": towns.names[near],
                          "b_country": towns.countries[near]})
            if len(found) >= 500:
                await _write(session, found)
                counts["named"] += len(found)
                found = []
        if found:
            await _write(session, found)
            counts["named"] += len(found)
        if far:
            await _write(session, far)
            counts["too_far"] = len(far)
    return counts


async def _write(session, found: list[dict]) -> None:
    await session.execute(
        Photo.__table__.update()
        .where(Photo.id == bindparam("b_id"))
        .values(place=bindparam("b_place"), country=bindparam("b_country"),
                place_source="geocoded"), found)
    await session.commit()
