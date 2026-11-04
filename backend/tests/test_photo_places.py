import datetime

import pytest
from sqlalchemy import select

from conftest import library, run, signed_in
from opus import db
from opus.models import Photo, PhotoFile
from opus.photos.library import metadata, places

TOWNS = [
    # id, name, lat, lon, feature, country, admin1, population
    (3196915, "Kupres", 43.99273, 17.27639, "PPLA3", "BA", "01", 3000),
    (3183646, "Zejmen", 41.70778, 19.68917, "PPL", "AL", "40", 900),
    (3337001, "Zaton", 42.68, 18.04, "PPL", "HR", "03", 900),
    (3337002, "Zaton", 44.23, 15.17, "PPL", "HR", "19", 600),
    (1485000, "Zaton", 52.0, 85.0, "PPL", "RU", "04", 9000),
    (1485001, "Zatonsk", 53.0, 86.0, "PPL", "RU", "04", 90000),
    (2950159, "Mitte", 52.52, 13.40, "PPLX", "DE", "16", 100000),
]
REGIONS = {"BA.01": "Federation of B&H", "HR.03": "Dubrovnik-Neretva",
           "HR.19": "Zadar", "AL.40": "Shkodër", "RU.04": "Altai Krai"}


@pytest.fixture
def towns(tmp_path, monkeypatch):
    kept, regions = tmp_path / "cities500.txt", tmp_path / "admin1.txt"
    kept.write_text("".join(
        "\t".join([str(i), name, name, "", str(lat), str(lon), "P", feature, cc, "", a1,
                   "", "", "", str(people), "", "0", "Europe/Zagreb", "2024-01-01"]) + "\n"
        for i, name, lat, lon, feature, cc, a1, people in TOWNS), encoding="utf-8")
    regions.write_text("".join(f"{k}\t{v}\t{v}\t1\n" for k, v in REGIONS.items()),
                       encoding="utf-8")
    known = places._read(kept, regions)
    monkeypatch.setattr(places, "_held", known)
    return known


def test_a_name_finds_the_towns_called_that_first_and_where_the_archive_has_been(towns):
    found = towns.find("zaton", {"HR": 40})
    assert [(t.name, t.country, t.region) for t in found] == [
        ("Zaton", "HR", "Dubrovnik-Neretva"), ("Zaton", "HR", "Zadar"),
        ("Zaton", "RU", "Altai Krai"), ("Zatonsk", "RU", "Altai Krai")]
    assert [t.country for t in towns.find("ZATON", {})] == ["RU", "HR", "HR", "RU"]
    assert towns.find("mitte", {}) == []
    assert towns.find("  ", {}) == []
    assert towns.at(3196915) == places.Settlement(
        3196915, "Kupres", "BA", "Federation of B&H", 43.99273, 17.27639)
    assert towns.at(1) is None


async def _winter():
    """Six photographs from one day on Kupres that the camera put in Albania,
    one of them already named by hand, and one from the next day."""
    async with db.SessionLocal() as session:
        def photo(n: int, hour: int, day: int = 5, **fields):
            row = Photo(checksum=bytes([n]) * 20, kind="image",
                        taken_at=datetime.datetime(2009, 1, day, hour, tzinfo=datetime.UTC),
                        latitude=41.7188, longitude=19.7054, altitude=12.0,
                        **({"place": "Zejmen", "country": "AL", "place_source": "geocoded"}
                           | fields))
            row.files.append(PhotoFile(path=f"/lib/2009/01/IMG_{n}.jpg"))
            session.add(row)
            return row

        rows = [photo(1, 9), photo(2, 10), photo(3, 11),
                photo(4, 12, place="Vikendica", country="", place_source="typed"),
                photo(5, 13, day=6)]
        await session.commit()
        return [r.checksum.hex() for r in rows]


async def _rows():
    async with db.SessionLocal() as session:
        return {p.checksum.hex(): p for p in (await session.execute(select(Photo))).scalars()}


def test_a_typed_town_brings_its_country_and_coordinates_to_the_whole_day(clean, towns):
    first, second, third, typed, next_day = run(_winter())

    async def scenario():
        async with library(await signed_in("boss")) as client:
            whole = await client.put(f"/api/photos/{first}/place",
                                     json={"settlement": 3196915, "day": True})
            unknown = await client.put(f"/api/photos/{first}/place", json={"settlement": 1})
            one = await client.put(f"/api/photos/{typed}/place", json={"settlement": 3196915})
            offered = await client.get("/api/photos/settlements", params={"q": "kup"})
        return whole, unknown, one, offered

    whole, unknown, one, offered = run(scenario())
    assert whole.status_code == 200, whole.text
    assert whole.json() == {"place": "Kupres", "country": "BA", "photographs": 3}
    assert unknown.status_code == 422
    assert one.json()["photographs"] == 1
    assert offered.json()[0] == {"id": 3196915, "name": "Kupres", "country": "BA",
                                 "region": "Federation of B&H",
                                 "latitude": 43.99273, "longitude": 17.27639}
    got = run(_rows())
    for checksum in (first, second, third, typed):
        p = got[checksum]
        assert (p.place, p.country, p.place_source) == ("Kupres", "BA", "typed")
        assert (p.latitude, p.longitude, p.altitude) == (43.99273, 17.27639, None)
    assert (got[next_day].place, got[next_day].latitude) == ("Zejmen", 41.7188)


def test_a_place_spread_across_a_day_leaves_one_typed_by_hand(clean, towns):
    first, *_, typed, _ = run(_winter())

    async def scenario():
        async with library(await signed_in("boss")) as client:
            return await client.put(f"/api/photos/{first}/place",
                                    json={"settlement": 3196915, "day": True})

    assert run(scenario()).json()["photographs"] == 3
    assert run(_rows())[typed].place == "Vikendica"


def test_reading_the_file_again_does_not_put_the_cameras_coordinates_back(clean, towns, monkeypatch):
    first, second, *_ = run(_winter())

    async def scenario():
        async with library(await signed_in("boss")) as client:
            await client.put(f"/api/photos/{first}/place", json={"settlement": 3196915})

    run(scenario())
    exif = {"DateTimeOriginal": "2009:01:05 10:00:00", "GPSLatitude": 41.7188,
            "GPSLongitude": 19.7054, "GPSAltitude": 12}
    monkeypatch.setattr(metadata, "_exiftool", lambda paths: {p: exif for p in paths})
    metadata.job.state.update(metadata.job._reset())
    run(metadata.read(True))

    got = run(_rows())
    assert (got[first].latitude, got[first].longitude, got[first].altitude) == (
        43.99273, 17.27639, None)
    assert (got[second].latitude, got[second].altitude) == (41.7188, 12.0)
