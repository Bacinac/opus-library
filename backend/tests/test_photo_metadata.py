import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import FILE_MISSING, TIME_EXIF, Photo, PhotoFile
from opus.photos.library import metadata

ZAGREB = ZoneInfo("Europe/Zagreb")

ROWS = {
    "/lib/2019/05/IMG_1.HEIC": {
        "DateTimeOriginal": "2019:05:04 10:11:12", "OffsetTimeOriginal": "+02:00",
        "Make": "Apple", "Model": "iPhone 8", "ImageWidth": 4000, "ImageHeight": 3000,
        "Orientation": 6, "GPSLatitude": 45.8, "GPSLongitude": 16.0, "GPSAltitude": 120,
        "LensID": "iPhone 8 back camera", "FocalLength": "4.0 mm", "FNumber": 1.8,
        "ISO": 50, "ExposureTime": 0.008, "ContentIdentifier": "LIVE-A",
    },
    "/lib/2019/05/IMG_1.MOV": {"CreateDate": "2019:05:04 10:11:12",
                               "ContentIdentifier": "LIVE-A"},
    "/lib/misc/nothing.jpg": {"GPSLatitude": 0, "GPSLongitude": 0},
}


async def _seed():
    async with db.SessionLocal() as session:
        def photo(n: int, *files, kind="image", **fields):
            row = Photo(checksum=bytes([n]) * 20, kind=kind, **fields)
            session.add(row)
            for path, extra in files:
                row.files.append(PhotoFile(path=path, **extra))
            return row

        rows = [
            photo(1, ("/lib/2019/05/IMG_1.HEIC", {})),
            photo(2, ("/lib/2019/05/IMG_1.MOV", {}), kind="video"),
            photo(3, ("/lib/x/IMG_20130706_154433.jpg", {}), ("/lib/y/20120101-101010.jpg", {}),
                  device_make="Canon"),
            photo(4, ("/lib/2015/03/a.jpg", {}), ("/lib/1970/01/b.jpg", {})),
            photo(5, ("/lib/misc/c.jpg", {"mtime_ns": 1_700_000_000_000_000_000}),
                  ("/lib/misc/d.jpg", {"mtime_ns": 1_600_000_000_000_000_000})),
            photo(6, ("/lib/misc/nothing.jpg", {})),
            photo(7, ("/lib/z/IMG_20130706_154433.jpg", {}), taken_source=TIME_EXIF,
                  taken_at=datetime.datetime(2001, 1, 1, tzinfo=datetime.UTC)),
            photo(8, ("/lib/gone.jpg", {"state": FILE_MISSING})),
        ]
        await session.commit()
        return [r.id for r in rows]


async def _read():
    async with db.SessionLocal() as session:
        return {p.id: p for p in (await session.execute(select(Photo))).scalars()}


def test_every_photograph_is_dated_by_the_best_answer_it_has(clean, monkeypatch):
    monkeypatch.setattr(metadata, "_exiftool", lambda paths: {p: ROWS[p] for p in paths if p in ROWS})
    ids = run(_seed())
    metadata.job.state.update(metadata.job._reset())

    run(metadata.read(True))

    got = run(_read())
    still, clip, named, filed, stamped, bare, known, gone = (got[i] for i in ids)
    assert (still.taken_at, still.taken_offset, still.taken_source) == (
        datetime.datetime(2019, 5, 4, 8, 11, 12, tzinfo=datetime.UTC), "+02:00", "exif")
    assert (still.device_make, still.device_model, still.pixel_w, still.pixel_h) == (
        "Apple", "iPhone 8", 3000, 4000)
    assert (still.latitude, still.longitude, still.altitude) == (45.8, 16.0, 120.0)
    assert (still.lens, still.focal_mm, still.aperture, still.iso, still.shutter) == (
        "iPhone 8 back camera", 4.0, 1.8, 50, "0.008")
    assert (still.live_pair_id, clip.live_pair_id) == (clip.id, still.id)
    assert (clip.taken_at, clip.taken_offset) == (
        datetime.datetime(2019, 5, 4, 10, 11, 12, tzinfo=ZAGREB), "")
    assert (named.taken_at, named.taken_source, named.device_make) == (
        datetime.datetime(2012, 1, 1, 10, 10, 10, tzinfo=ZAGREB), "filename", "Canon")
    assert (filed.taken_at, filed.taken_source) == (
        datetime.datetime(2015, 3, 1, tzinfo=ZAGREB), "path")
    assert (stamped.taken_at, stamped.taken_source) == (
        datetime.datetime(2020, 9, 13, 12, 26, 40, tzinfo=datetime.UTC), "mtime")
    assert (bare.taken_at, bare.taken_source, bare.latitude) == (None, "none", None)
    assert (known.taken_at, known.taken_source) == (
        datetime.datetime(2001, 1, 1, tzinfo=datetime.UTC), "exif")
    assert (gone.taken_source, gone.live_token) == ("none", None)
    counts = {k: metadata.job.state[k] for k in (
        "total", "processed", "dated", "from_exif", "from_filename", "from_path",
        "from_mtime", "undated", "paired")}
    assert counts == {"total": 8, "processed": 8, "dated": 6, "from_exif": 2,
                      "from_filename": 2, "from_path": 1, "from_mtime": 1, "undated": 1,
                      "paired": 1}
