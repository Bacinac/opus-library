import datetime
import json
import subprocess

import pytest
import pyvips
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from conftest import library, run, signed_in
from opus import db, settings_store
from opus.models import Photo, PhotoFile, PhotoShare, Setting
from opus.photos.library import derive
from scene import Scene


def tags(data: bytes) -> dict:
    return json.loads(subprocess.run(["exiftool", "-j", "-G1", "-a", "-n", "-"], input=data,
                                     capture_output=True, check=True).stdout)[0]


@pytest.fixture
def album(clean, tmp_path):
    """A photograph that knows where and when it was taken and on what, and a
    recording beside it."""
    source = tmp_path / "IMG_0001.jpg"
    pyvips.Image.black(400, 200).jpegsave(str(source))
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Make=Apple", "-Model=iPhone 13",
                    "-DateTimeOriginal=2019:07:14 18:30:00", "-Orientation#=1",
                    "-GPSLatitude=43.5081", "-GPSLatitudeRef=N",
                    "-GPSLongitude=16.4402", "-GPSLongitudeRef=E", str(source)], check=True)
    assert "GPS:GPSLatitude" in tags(source.read_bytes())
    scene = Scene()

    async def build():
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(
                key="photos_derivatives_dir", value=str(tmp_path / "derivatives")))
            photo_id = await scene.photo(session, datetime.datetime(2019, 7, 14, tzinfo=datetime.UTC))
            photo = await session.get(Photo, photo_id)
            photo.pixel_w, photo.pixel_h = 400, 200
            session.add(PhotoFile(photo_id=photo_id, path=str(source)))
            clip = await session.get(Photo, await scene.photo(session, None))
            clip.kind = "video"
            await session.commit()
            return photo.checksum.hex(), clip.checksum.hex()

    photo, clip = run(build())
    settings_store.forget_runtime()
    tile, _ = derive.paths_for(tmp_path / "derivatives", bytes.fromhex(photo))
    tile.parent.mkdir(parents=True)
    pyvips.Image.black(350, 175).jpegsave(str(tile))
    return photo, clip, run(signed_in("boss"))


def made(album, days: int = 7):
    photo, _, owner = album

    async def scenario():
        async with library(owner) as client:
            return (await client.post("/api/photos/shares",
                                      json={"photos": [photo], "days": days})).json()
    return run(scenario())


def test_a_link_opens_the_chosen_picture_and_nothing_about_it(album):
    photo, *_ = album
    share = made(album)

    async def scenario():
        async with library() as stranger:
            listed = await stranger.get(f"/api/shared/{share['key']}")
            tile = await stranger.get(f"/api/shared/{share['key']}/0/tile")
            return listed, tile

    listed, tile = run(scenario())
    assert listed.status_code == 200, listed.text
    [shown] = listed.json()["photos"]
    assert (shown["id"], shown["taken_at"], shown["w"], shown["h"]) == ("0", None, 400, 200)
    assert photo not in listed.text
    assert tile.status_code == 200
    assert "immutable" not in tile.headers["cache-control"]


def test_the_original_leaves_without_where_when_or_on_what(album):
    photo, *_ = album
    share = made(album)

    async def scenario():
        async with library() as stranger:
            return await stranger.get(f"/api/shared/{share['key']}/0/original")

    answer = run(scenario())
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-disposition"].endswith('-001.jpg"')
    left = tags(answer.content)
    assert not [t for t in left if t.startswith(("GPS:", "IFD0:Make", "ExifIFD:"))]
    assert left["IFD0:Orientation"] == 1
    assert pyvips.Image.new_from_buffer(answer.content, "").width == 400


def test_a_picture_turned_by_hand_arrives_standing(album):
    photo, *_ = album

    async def turn():
        async with db.SessionLocal() as session:
            await session.execute(update(Photo).where(Photo.checksum == bytes.fromhex(photo))
                                  .values(turn=90))
            await session.commit()

    run(turn())
    share = made(album)

    async def scenario():
        async with library() as stranger:
            return await stranger.get(f"/api/shared/{share['key']}/0/original")

    assert tags(run(scenario()).content)["IFD0:Orientation"] == 6


def test_a_withdrawn_link_and_an_expired_one_open_nothing(album):
    photo, _, owner = album
    withdrawn, expired = made(album), made(album)

    async def scenario():
        async with library(owner) as client:
            gone = await client.delete(f"/api/photos/shares/{withdrawn['id']}")
        async with db.SessionLocal() as session:
            await session.execute(update(PhotoShare).where(PhotoShare.id == expired["id"])
                                  .values(expires_at=datetime.datetime.now(datetime.UTC)))
            await session.commit()
        async with library() as stranger:
            return gone, [(await stranger.get(f"/api/shared/{s['key']}{tail}")).status_code
                          for s in (withdrawn, expired) for tail in ("", "/0/tile", "/0/original")]

    gone, answers = run(scenario())
    assert gone.status_code == 200
    assert answers == [404] * 6


def test_only_the_admin_makes_and_sees_links_and_a_stranger_only_reads(album):
    photo, *_ = album
    share = made(album)

    async def scenario():
        async with library(await signed_in("ana", role="user")) as member:
            makes = await member.post("/api/photos/shares", json={"photos": [photo], "days": 7})
            sees = await member.get("/api/photos/shares")
        async with library() as stranger:
            lists = await stranger.get("/api/photos/shares")
            writes = await stranger.delete(f"/api/shared/{share['key']}")
        return makes, sees, lists, writes

    makes, sees, lists, writes = run(scenario())
    assert (makes.status_code, sees.status_code) == (403, 403)
    assert lists.status_code == 401
    assert writes.status_code in (401, 405)


def test_a_recording_is_not_handed_out_by_link(album):
    photo, clip, owner = album

    async def scenario():
        async with library(owner) as client:
            return await client.post("/api/photos/shares", json={"photos": [photo, clip], "days": 7})

    assert run(scenario()).status_code == 400


def test_the_owner_sees_what_is_out_and_until_when(album):
    photo, _, owner = album
    share = made(album, days=30)

    async def scenario():
        async with library(owner) as client:
            return (await client.get("/api/photos/shares")).json()

    [listed] = run(scenario())
    assert (listed["id"], listed["photos"], listed["made_by"]) == (share["id"], 1, "boss")
    assert listed["cover"]["id"] == photo
    assert listed["expires_at"] == share["expires_at"]
