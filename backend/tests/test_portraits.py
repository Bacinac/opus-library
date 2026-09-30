import asyncio
import datetime
import hashlib
import itertools
import json

import httpx
import numpy as np
import pytest
import pyvips
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

import opus_auth
from conftest import library, run, signed_in
from opus import auth, db, settings_store
from opus.models import FACE_DIMS, FACE_GENERATION, Face, Person, Photo, Setting
from opus.photos.library import derive

TOKEN = "module-token-0123456789"
MARKS = [0.4, 0.4, 0.6, 0.4, 0.5, 0.5, 0.42, 0.62, 0.58, 0.62]
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 fake"


_counted = itertools.count()


class Faces:
    def __init__(self):
        self.asked: list[tuple[str, dict]] = []
        self.hold: asyncio.Event | None = None
        self.down = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        body = json.loads(request.content)
        self.asked.append((request.url.path, body))
        if self.hold is not None:
            await self.hold.wait()
        if request.url.path == "/landmarks":
            return httpx.Response(200, json={"results": [
                {"id": f["id"], "landmarks": MARKS} for f in body["faces"]]})
        return httpx.Response(200, content=WEBP, headers={"X-OPUS-Frames": "3/3"})


@pytest.fixture
def faces(monkeypatch):
    service = Faces()
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(service))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return service


@pytest.fixture
def store(tmp_path, quick):
    root = tmp_path / "derivatives"

    async def configure():
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(
                key="photos_derivatives_dir", value=str(root)))
            await session.commit()

    run(configure())
    return root


async def _picture(session, root, year: int) -> Photo:
    checksum = hashlib.sha1(f"picture {next(_counted)}".encode()).digest()
    photo = Photo(checksum=checksum, kind="image", pixel_w=2048,
                  taken_at=datetime.datetime(year, 5, 1, 12, tzinfo=datetime.UTC))
    session.add(photo)
    await session.flush()
    preview = derive.paths_for(root, checksum)[1]
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(pyvips.Image.black(600, 600).write_to_buffer(".jpg"))
    return photo


async def _person(root, years=(2019, 2020, 2021), marked=True) -> tuple[int, list[int]]:
    rng = np.random.default_rng(3)
    async with db.SessionLocal() as session:
        n = next(_counted)
        person = Person(name=f"Kata Test {n}", given_name="Kata", family_name=f"Test {n}")
        session.add(person)
        await session.flush()
        faces = []
        for year in years:
            photo = await _picture(session, root, year)
            vector = rng.normal(size=FACE_DIMS)
            face = Face(photo_id=photo.id, person_id=person.id, x=0.3, y=0.3, w=0.3, h=0.3,
                        score=0.9, sharpness=5.0, generation=FACE_GENERATION,
                        embedding=(vector / np.linalg.norm(vector)).tolist(),
                        landmarks=MARKS if marked else None)
            session.add(face)
            await session.flush()
            faces.append(face.id)
        await session.commit()
        return person.id, faces


def test_a_face_is_cut_out_once_and_kept(store, faces):
    async def scenario():
        _, (face, *_) = await _person(store)
        async with library(await signed_in("filip", role="user")) as client:
            small = await client.get(f"/api/photos/faces/{face}/crop")
            big = await client.get(f"/api/photos/faces/{face}/crop", params={"size": "big"})
            again = await client.get(f"/api/photos/faces/{face}/crop")
            nobody = await client.get("/api/photos/faces/999999/crop")
        return face, small, big, again, nobody

    face, small, big, again, nobody = run(scenario())
    assert small.status_code == 200 and small.headers["content-type"] == "image/jpeg"
    assert pyvips.Image.new_from_buffer(small.content, "").width == 256
    assert pyvips.Image.new_from_buffer(big.content, "").width == 512
    assert again.content == small.content
    assert (store / "crop" / f"{face % 256:02x}" / f"{face}-g{derive.GENERATION}-256.jpg").exists()
    assert nobody.status_code == 404


def test_a_portrait_needs_its_landmarks(store, faces):
    async def scenario():
        _, (marked, *_) = await _person(store)
        async with db.SessionLocal() as session:
            bare = (await session.execute(select(Face).where(Face.id == marked))).scalar_one()
            bare_photo = await _picture(session, store, 2022)
            unmarked = Face(photo_id=bare_photo.id, x=0.3, y=0.3, w=0.3, h=0.3, score=0.9,
                            generation=FACE_GENERATION, embedding=bare.embedding)
            session.add(unmarked)
            await session.commit()
            unmarked_id = unmarked.id
        async with library(await signed_in("filip", role="user")) as client:
            return (await client.get(f"/api/photos/faces/{marked}/portrait"),
                    await client.get(f"/api/photos/faces/{unmarked_id}/portrait"))

    portrait, refused = run(scenario())
    assert portrait.status_code == 200
    image = pyvips.Image.new_from_buffer(portrait.content, "")
    assert (image.width, image.height) == (448, 448)
    assert refused.status_code == 422


def test_a_transformation_asks_for_missing_landmarks_once(store, faces):
    async def scenario():
        person, ids = await _person(store, marked=False)
        async with library(await signed_in("filip", role="user")) as client:
            first = await client.get(f"/api/photos/people/{person}/transformation")
            second = await client.get(f"/api/photos/people/{person}/transformation")
        async with db.SessionLocal() as session:
            stored = (await session.execute(
                select(Face.landmarks).where(Face.id.in_(ids)))).scalars().all()
        return ids, first, second, stored

    ids, first, second, stored = run(scenario())
    assert first.status_code == 200
    run_ = first.json()
    assert [f["year"] for f in run_["frames"]] == [2019, 2020, 2021]
    assert [f["face"] for f in run_["frames"]] == ids
    assert run_["morph"] == {"ms": 60, "hold": 3, "steps": 10}
    assert second.json() == run_
    assert [path for path, _ in faces.asked] == ["/landmarks"]
    assert all(marks == MARKS for marks in stored)


def test_a_morph_is_made_once_and_served_from_the_store(store, faces):
    async def scenario():
        person, _ = await _person(store)
        async with library(await signed_in("filip", role="user")) as client:
            wrong_size = await client.get(f"/api/photos/people/{person}/morph", params={"size": 300})
            made = await client.get(f"/api/photos/people/{person}/morph", params={"size": 256})
            kept = await client.get(f"/api/photos/people/{person}/morph", params={"size": 256})
        return person, wrong_size, made, kept

    person, wrong_size, made, kept = run(scenario())
    assert wrong_size.status_code == 400
    assert made.status_code == 200 and made.content == WEBP
    assert made.headers["content-type"] == "image/webp"
    assert kept.content == WEBP
    morphs = [body for path, body in faces.asked if path == "/morph"]
    assert len(morphs) == 1
    assert (morphs[0]["size"], morphs[0]["steps"]) == (256, 10)
    assert [f["year"] for f in morphs[0]["faces"]] == [2019, 2020, 2021]
    assert len(list((store / "morphs").glob(f"{person}-*-256-10.webp"))) == 1


def test_one_year_has_nothing_to_become(store, faces):
    async def scenario():
        person, _ = await _person(store, years=(2020,))
        async with library(await signed_in("filip", role="user")) as client:
            return await client.get(f"/api/photos/people/{person}/morph")

    assert run(scenario()).status_code == 422
    assert faces.asked == []


def test_an_unreachable_faces_service_is_said(store, faces):
    faces.down = True

    async def scenario():
        person, _ = await _person(store)
        async with library(await signed_in("filip", role="user")) as client:
            return await client.get(f"/api/photos/people/{person}/morph")

    assert run(scenario()).status_code == 503


def test_only_an_admin_chooses_how_a_morph_moves(store, faces):
    async def scenario():
        person, _ = await _person(store)
        answers = {}
        async with library(await signed_in("filip", role="user")) as client:
            answers["user"] = await client.get(f"/api/photos/people/{person}/morph",
                                               params={"steps": 4})
            answers["user, the usual"] = await client.get(f"/api/photos/people/{person}/morph",
                                                          params={"steps": 10})
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(key=auth.token_key("player"), value=TOKEN))
            await session.commit()
        settings_store.forget_runtime()
        async with library() as client:
            client.headers[opus_auth.TOKEN_HEADER] = TOKEN
            answers["module"] = await client.get(f"/api/photos/people/{person}/morph",
                                                 params={"steps": 4})
        async with library(await signed_in("boss")) as client:
            answers["admin"] = await client.get(f"/api/photos/people/{person}/morph",
                                                params={"steps": 4})
            answers["admin, out of range"] = await client.get(
                f"/api/photos/people/{person}/morph", params={"steps": 11})
        return answers

    answers = run(scenario())
    assert answers["user"].status_code == 403
    assert answers["module"].status_code == 403
    assert answers["user, the usual"].status_code == 200
    assert answers["admin"].status_code == 200
    assert answers["admin, out of range"].status_code == 400
    assert sorted(body["steps"] for path, body in faces.asked if path == "/morph") == [4, 10]


def test_the_card_takes_only_so_much_at_once(store, faces):
    from opus.photos.people import portraits

    faces.hold = asyncio.Event()

    async def scenario():
        people = [(await _person(store, years=(2019, 2020)))[0]
                  for _ in range(portraits.GPU_AT_ONCE + 1)]
        cookie = await signed_in("filip", role="user")
        async with library(cookie) as client:
            working = [asyncio.create_task(client.get(f"/api/photos/people/{p}/morph"))
                       for p in people[:-1]]
            while len(faces.asked) < portraits.GPU_AT_ONCE:
                await asyncio.sleep(0.01)
            full = await client.get(f"/api/photos/people/{people[-1]}/morph")
            faces.hold.set()
            done = await asyncio.gather(*working)
            after = await client.get(f"/api/photos/people/{people[-1]}/morph")
        return full, done, after

    full, done, after = run(scenario())
    assert full.status_code == 503
    assert [d.status_code for d in done] == [200] * portraits.GPU_AT_ONCE
    assert after.status_code == 200
