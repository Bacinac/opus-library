import asyncio
import datetime
import hashlib
import os
import time

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from conftest import library, run, signed_in
from opus import db
from opus.models import Photo, PhotoFile, PhotoOffer, Setting
from opus.photos import room
from opus.photos.library import offers, place
from opus.settings_store import current_runtime


@pytest.fixture
def tree(tmp_path, monkeypatch, quick):
    monkeypatch.setattr(room, "KEEP_FREE", 0)
    seen, write = tmp_path / "photos", tmp_path / "photos-write"
    seen.mkdir()
    (write / offers.WAITING).mkdir(parents=True)

    async def configure():
        async with db.SessionLocal() as session:
            for key, value in (("photos_dir", str(seen)), ("photos_write_dir", str(write))):
                await session.execute(insert(Setting).values(key=key, value=value))
            await session.commit()

    run(configure())
    return write / offers.WAITING


def arrived(folder) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def test_a_name_on_the_disk_is_never_replaced(tree):
    (tree / "IMG_0001.JPG").write_bytes(b"first")

    async def scenario():
        sources = []
        for n in range(3):
            source = tree / f".{n}.arriving"
            source.write_bytes(f"later {n}".encode())
            sources.append(source)
        async with db.SessionLocal() as a, db.SessionLocal() as b, db.SessionLocal() as c:
            landed = await asyncio.gather(*(
                offers.land(session, source, tree, tree, "IMG_0001.JPG",
                            hashlib.sha1(source.read_bytes()).digest())
                for session, source in zip((a, b, c), sources)))
        for source in sources:
            source.unlink()
        return landed

    landed = run(scenario())
    assert sorted(p.name for p in landed) == ["IMG_0001-1.JPG", "IMG_0001-2.JPG", "IMG_0001-3.JPG"]
    files = arrived(tree)
    assert files.pop("IMG_0001.JPG") == b"first"
    assert sorted(files.values()) == [b"later 0", b"later 1", b"later 2"]


def test_a_name_the_catalogue_holds_is_not_free(tree):
    async def scenario():
        async with db.SessionLocal() as session:
            photo = Photo(checksum=hashlib.sha1(b"gone").digest())
            session.add(photo)
            await session.flush()
            session.add(PhotoFile(photo_id=photo.id, path=str(tree / "holiday.jpg")))
            await session.commit()
            source = tree / ".x.arriving"
            source.write_bytes(b"new")
            landed = await offers.land(session, source, tree, tree, "holiday.jpg",
                                       hashlib.sha1(b"new").digest())
            source.unlink()
            return landed

    assert run(scenario()).name == "holiday-1.jpg"
    assert arrived(tree) == {"holiday-1.jpg": b"new"}


def test_the_same_picture_given_twice_at_once_lands_once(tree):
    checksum = hashlib.sha1(b"the picture").digest()

    async def give(person: str, n: int):
        source = tree / f".{n}.arriving"
        source.write_bytes(b"the picture")
        config = await current_runtime()
        async with db.SessionLocal() as session:
            try:
                return await offers.give(session, source, config, "IMG.jpg", checksum, person, None)
            finally:
                source.unlink(missing_ok=True)

    async def scenario():
        return await asyncio.gather(give("filip", 1), give("jana", 2))

    landed = run(scenario())
    assert sorted(p is None for p in landed) == [False, True]
    assert arrived(tree) == {"IMG.jpg": b"the picture"}

    async def notes():
        async with db.SessionLocal() as session:
            return (await session.execute(select(PhotoOffer.checksum))).scalars().all()

    assert run(notes()) == [checksum]


def test_offering_over_the_door(tree):
    async def scenario():
        cookie = await signed_in("filip", role="user")
        async with library(cookie) as client:
            same = await asyncio.gather(*(
                client.post("/api/photos/offer", params={"name": "IMG_0001.JPG"},
                            content=b"one picture" * 1000)
                for _ in range(2)))
            other = await client.post("/api/photos/offer", params={"name": "IMG_0001.JPG"},
                                      content=b"another picture")
            empty = await client.post("/api/photos/offer", params={"name": "x.jpg"}, content=b"")
        return same, other, empty

    same, other, empty = run(scenario())
    assert [r.status_code for r in same] == [200, 200]
    answers = sorted((r.json() for r in same), key=lambda a: a["known"])
    assert answers[0]["known"] is False and answers[0]["bytes"] == 11000
    assert answers[1] == {"known": True, "pending": True}
    assert other.status_code == 200 and other.json()["at"].endswith("IMG_0001-1.JPG")
    assert empty.status_code == 400
    assert arrived(tree) == {"IMG_0001.JPG": b"one picture" * 1000,
                             "IMG_0001-1.JPG": b"another picture"}
    assert not [name for name in os.listdir(tree) if name.startswith(".")]


def test_a_file_over_the_largest_is_refused_announced_or_not(tree, monkeypatch):
    monkeypatch.setattr(room, "LARGEST", 100)

    async def unannounced():
        for _ in range(3):
            yield b"x" * 40

    async def scenario():
        cookie = await signed_in("filip", role="user")
        async with library(cookie) as client:
            return [await client.post("/api/photos/offer", params={"name": "big.mov"}, content=body)
                    for body in (b"x" * 101, unannounced(), b"x" * 100)]

    said, streamed, largest = run(scenario())
    assert said.status_code == 413 and streamed.status_code == 413
    assert largest.status_code == 200
    assert arrived(tree) == {"big.mov": b"x" * 100}


def test_an_offer_cut_off_before_its_note_is_finished_by_the_next_one(tree):
    checksum = hashlib.sha1(b"the picture").digest()
    source = tree / ".1.arriving"
    source.write_bytes(b"the picture")

    async def cut_off():
        config = await current_runtime()
        async with db.SessionLocal() as session:
            async def crash():
                raise RuntimeError("the process was killed")

            session.commit = crash
            with pytest.raises(RuntimeError):
                await offers.give(session, source, config, "IMG.jpg", checksum, "filip", None)

    async def again():
        config = await current_runtime()
        async with db.SessionLocal() as session:
            landed = await offers.give(session, source, config, "IMG.jpg", checksum, "filip", None)
        async with db.SessionLocal() as session:
            note = await session.get(PhotoOffer, checksum)
        return landed, note

    run(cut_off())
    assert source.exists()
    assert (tree / "IMG.jpg").read_bytes() == b"the picture"
    landed, note = run(again())
    assert landed == tree / "IMG.jpg"
    assert note is not None and note.person == "filip"
    assert not source.exists()
    assert arrived(tree) == {"IMG.jpg": b"the picture"}


def test_the_pass_sees_the_note_of_an_offer_still_landing(tree, monkeypatch):
    checksum = hashlib.sha1(b"the picture").digest()
    source = tree / ".1.arriving"
    source.write_bytes(b"the picture")
    seen = {}
    land = offers.land

    async def slow_land(*args, **kwargs):
        target = await land(*args, **kwargs)
        seen["reader"] = asyncio.ensure_future(_claim(checksum))
        await asyncio.sleep(0.3)
        seen["waited"] = not seen["reader"].done()
        return target

    async def _claim(checksum):
        async with db.SessionLocal() as session:
            photo = Photo(checksum=checksum)
            session.add(photo)
            await session.flush()
            who = await offers.claimed_by(session, checksum, photo.id)
            await session.commit()
            return who

    monkeypatch.setattr(offers, "land", slow_land)

    async def scenario():
        config = await current_runtime()
        async with db.SessionLocal() as session:
            await offers.give(session, source, config, "IMG.jpg", checksum, "jana", None)
        return await seen["reader"]

    assert run(scenario()) == "jana"
    assert seen["waited"]


def test_what_an_offer_left_half_written_is_swept_once_it_is_stale(tree):
    stale, fresh, picture = tree / ".a.arriving", tree / ".b.opening", tree / "IMG.jpg"
    for path in (stale, fresh, picture):
        path.write_bytes(b"x")
    hours_ago = time.time() - 2 * 3600
    os.utime(stale, (hours_ago, hours_ago))
    os.utime(picture, (hours_ago, hours_ago))

    async def scenario():
        return offers.sweep(await current_runtime())

    assert run(scenario()) == 1
    assert sorted(p.name for p in tree.iterdir()) == [".b.opening", "IMG.jpg"]


async def _waiting(tree, name: str, content: bytes, taken: datetime.datetime | None) -> int:
    path = tree / name
    path.write_bytes(content)
    st = path.stat()
    config = await current_runtime()
    async with db.SessionLocal() as session:
        photo = Photo(checksum=hashlib.sha1(content).digest(), taken_at=taken)
        session.add(photo)
        await session.flush()
        row = PhotoFile(photo_id=photo.id, path=str(offers.waiting_seen(config) / name),
                        dev=st.st_dev, inode=st.st_ino, byte_size=st.st_size,
                        mtime_ns=st.st_mtime_ns)
        session.add(row)
        await session.commit()
        return row.id


async def _paths() -> dict[int, str]:
    async with db.SessionLocal() as session:
        return dict((await session.execute(select(PhotoFile.id, PhotoFile.path))).all())


def test_a_dated_picture_is_filed_under_its_year_and_month(tree):
    seen = tree.parent.parent / "photos"
    summer = datetime.datetime(2019, 7, 31, 23, 30, tzinfo=datetime.UTC)

    async def scenario():
        filed = await _waiting(tree, "IMG_1.jpg", b"summer", summer)
        undated = await _waiting(tree, "IMG_2.jpg", b"whenever", None)
        return filed, undated, await place.settle(), await _paths()

    filed, undated, said, paths = run(scenario())
    assert said == {"placed": 1, "undated": 1, "refused": 0, "swept": 0}
    assert paths[filed] == str(seen / "2019" / "08" / "IMG_1.jpg")
    assert paths[undated] == str(seen / offers.WAITING / "IMG_2.jpg")
    assert (tree.parent / "2019" / "08" / "IMG_1.jpg").read_bytes() == b"summer"
    assert arrived(tree) == {"IMG_2.jpg": b"whenever"}


def test_filing_never_takes_a_name_another_picture_holds(tree):
    seen = tree.parent.parent / "photos"
    month = tree.parent / "2021" / "03"
    march = datetime.datetime(2021, 3, 2, 12, tzinfo=datetime.UTC)

    async def scenario():
        month.mkdir(parents=True)
        (month / "IMG.jpg").write_bytes(b"already filed")
        async with db.SessionLocal() as session:
            other = Photo(checksum=hashlib.sha1(b"already filed").digest())
            session.add(other)
            await session.flush()
            session.add(PhotoFile(photo_id=other.id, path=str(seen / "2021" / "03" / "IMG.jpg")))
            await session.commit()
        filed = await _waiting(tree, "IMG.jpg", b"the new one", march)
        await place.settle()
        return filed, await _paths()

    filed, paths = run(scenario())
    assert paths[filed] == str(seen / "2021" / "03" / "IMG-1.jpg")
    assert (month / "IMG.jpg").read_bytes() == b"already filed"
    assert (month / "IMG-1.jpg").read_bytes() == b"the new one"


def test_filing_cut_off_after_the_link_is_finished_without_a_copy(tree):
    seen = tree.parent.parent / "photos"
    month = tree.parent / "2021" / "03"
    march = datetime.datetime(2021, 3, 2, 12, tzinfo=datetime.UTC)

    async def scenario():
        filed = await _waiting(tree, "IMG.jpg", b"the picture", march)
        month.mkdir(parents=True)
        os.link(tree / "IMG.jpg", month / "IMG.jpg")
        await place.settle()
        return filed, await _paths()

    filed, paths = run(scenario())
    assert paths[filed] == str(seen / "2021" / "03" / "IMG.jpg")
    assert sorted(p.name for p in month.iterdir()) == ["IMG.jpg"]
    assert arrived(tree) == {}
