"""Turning coordinates into the name of a place.

Eight thousand photographs carry a latitude and a longitude, which is not a
place: nobody looks for 43.06, 17.42, they look for Podgora. The name is worked
out here, once, from a list of the world's settlements — offline, in this
container, because a lookup service would mean sending the coordinates of a
private archive to somebody else's machine and depending on their quota to
search our own photographs.

It never touches a place somebody typed. A name a person put on a photograph is
the best fact available about where they were; this is the second best, and the
second best does not overwrite the first.

The list is GeoNames' cities500 — every settlement over five hundred people,
about two hundred thousand of them, thirteen megabytes fetched once. Under that
size a village stops being in the list, and the honest answer for a photograph
taken there is the nearest place that is.
"""

import asyncio
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
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

SOURCE = "https://download.geonames.org/export/dump/cities500.zip"

# How far the nearest settlement may be before a photograph is left unnamed. A
# picture taken at sea or from an aeroplane is nearest to somewhere, and naming
# it after a town fifty kilometres away would be worse than saying nothing.
REACH_KM = 40.0


async def _settlements(root: pathlib.Path) -> tuple[np.ndarray, np.ndarray, list, list]:
    """The world's towns, fetched once and kept beside the derivatives."""
    kept = root / "places" / "cities500.txt"
    if not kept.exists():
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            resp = await client.get(SOURCE)
            resp.raise_for_status()
        await asyncio.to_thread(_unpack, resp.content, kept)
    return await asyncio.to_thread(_read, kept)


def _unpack(bundle: bytes, kept: pathlib.Path) -> None:
    kept.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(bundle)) as opened:
        kept.write_bytes(opened.read("cities500.txt"))


def _read(kept: pathlib.Path) -> tuple[np.ndarray, np.ndarray, list, list]:
    lats, lons, names, countries = [], [], [], []
    with kept.open(encoding="utf-8") as lines:
        for line in lines:
            bits = line.split("\t")
            if len(bits) < 9:
                continue
            # A district is not a place. GeoNames marks a section of a town PPLX,
            # and those sit closer to a photograph than the town does — which is
            # how an evening in Berlin came back as Mitte and Schöneberg, and a
            # week in Vienna as Hietzing. Everything else stays, so a village
            # keeps its own name instead of being swallowed by the city near it.
            if bits[7] == "PPLX":
                continue
            try:
                lats.append(float(bits[4]))
                lons.append(float(bits[5]))
            except ValueError:
                continue
            names.append(bits[1])
            countries.append(bits[8])
    return (np.radians(np.array(lats, np.float64)),
            np.radians(np.array(lons, np.float64)), names, countries)


async def run() -> dict:
    """Name every located photograph that has not been asked about. One too far
from anywhere is written down as asked, with no name."""
    counts = {"named": 0, "too_far": 0}
    async with SessionLocal() as session:
        config = await current_runtime()
        lat, lon, names, countries = await _settlements(derive.store_root(config))
        sin_lat, cos_lat = np.sin(lat), np.cos(lat)

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
            found.append({"b_id": row.id, "b_place": names[near][:120],
                          "b_country": countries[near][:2]})
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
