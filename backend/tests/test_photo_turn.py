import pytest
import pyvips
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from conftest import library, run, signed_in
from opus import db, settings_store
from opus.models import FACE_GENERATION, Face, Photo, PhotoFile, Setting
from opus.photos.library import derive
from opus.photos.library.metadata import _on_its_side
from scene import Scene


@pytest.fixture
def lying_down(clean, tmp_path):
    source = tmp_path / "IMG_0001.jpg"
    pyvips.Image.black(400, 200).jpegsave(str(source))
    scene = Scene()

    async def build():
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(
                key="photos_derivatives_dir", value=str(tmp_path / "derivatives")))
            photo_id = await scene.photo(session, None)
            photo = await session.get(Photo, photo_id)
            photo.pixel_w, photo.pixel_h, photo.faces_gen = 400, 200, FACE_GENERATION
            session.add(PhotoFile(photo_id=photo_id, path=str(source)))
            for _ in range(2):
                await scene.face(session, photo_id, scene.base(), "someone")
            await session.commit()
            return photo.checksum.hex()

    checksum = run(build())
    settings_store.forget_runtime()
    return checksum, tmp_path / "derivatives"


def test_a_turned_photograph_is_redrawn_standing_and_its_faces_are_looked_for_again(lying_down):
    checksum, root = lying_down

    async def scenario():
        async with library(await signed_in("boss")) as client:
            answer = await client.post(f"/api/photos/{checksum}/turn", json={"by": 90})
        async with db.SessionLocal() as session:
            photo = (await session.execute(
                select(Photo).where(Photo.checksum == bytes.fromhex(checksum)))).scalar_one()
            faces = await session.scalar(select(func.count()).select_from(Face)
                                         .where(Face.photo_id == photo.id))
            return answer, photo, faces

    answer, photo, faces = run(scenario())
    assert answer.status_code == 200, answer.text
    shape = answer.json()
    assert (shape["turn"], shape["w"], shape["h"]) == (90, 200, 400)
    assert (photo.turn, photo.pixel_w, photo.pixel_h, photo.faces_gen) == (90, 200, 400, 0)
    assert faces == 0
    tile, _ = derive.paths_for(root, bytes.fromhex(checksum))
    drawn = pyvips.Image.new_from_file(str(tile))
    assert drawn.height > drawn.width


def test_turning_is_the_libraries_upkeep_and_not_a_members(lying_down):
    checksum, _ = lying_down

    async def scenario():
        async with library(await signed_in("ana", role="user")) as client:
            return await client.post(f"/api/photos/{checksum}/turn", json={"by": 90})

    assert run(scenario()).status_code == 403


def test_a_file_on_its_side_is_read_as_the_shape_it_is_shown_in():
    assert _on_its_side({"Orientation": 6})
    assert _on_its_side({"Orientation": 8})
    assert _on_its_side({"Rotation": 270})
    assert not _on_its_side({"Orientation": 1})
    assert not _on_its_side({"Orientation": 3, "Rotation": 180})
    assert not _on_its_side({})
