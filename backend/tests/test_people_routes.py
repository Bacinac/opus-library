import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from conftest import library, run, signed_in
from opus import db, settings_store
from opus.models import Face, FaceCluster, Person, Setting
from opus.photos.people import cluster
from scene import Scene


@pytest.fixture
def household(quick, tmp_path):
    scene = Scene()
    known = run(scene.build())
    run(cluster.run())
    reels = tmp_path / "derivatives" / "morphs"
    reels.mkdir(parents=True)

    async def configure():
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(
                key="photos_derivatives_dir", value=str(tmp_path / "derivatives")))
            await session.commit()
        settings_store.forget_runtime()

    run(configure())
    return scene, known, reels


async def _groups(scene: Scene) -> dict[str, int]:
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(Face.id, Face.cluster_id)
                                      .where(Face.cluster_id.is_not(None)))).all()
    return {scene.labels[face]: group for face, group in rows}


async def _faces(scene: Scene, label: str) -> list[int]:
    return sorted(face for face, said in scene.labels.items() if said == label)


async def _state(faces: list[int]):
    async with db.SessionLocal() as session:
        return (await session.execute(
            select(Face.id, Face.person_id, Face.cluster_id)
            .where(Face.id.in_(faces)).order_by(Face.id))).all()


def _reel(reels, person_id: int):
    kept = reels / f"{person_id}-abc-448-10.webp"
    kept.write_bytes(b"reel")
    return kept


def test_a_person_is_added_once_by_name(household):
    async def scenario():
        async with library(await signed_in("boss")) as client:
            return [await client.post("/api/photos/people", json=body) for body in (
                {"name": " Cora Maria Test "},
                {"given_name": "Dora", "family_name": "Test", "born_on": "2011-02-03"},
                {"name": "cora maria test"},
                {"name": "  "})]

    cora, dora, again, empty = run(scenario())
    assert cora.status_code == 201
    assert {k: cora.json()[k] for k in ("name", "given_name", "family_name")} == {
        "name": "Cora Maria Test", "given_name": "Cora Maria", "family_name": "Test"}
    assert dora.json()["name"] == "Dora Test" and dora.json()["born_on"] == "2011-02-03"
    assert again.status_code == 409
    assert empty.status_code == 422


def test_a_birth_date_hands_back_the_groups_from_before_it(household):
    scene, known, reels = household

    async def scenario():
        groups = await _groups(scene)
        async with library(await signed_in("boss")) as client:
            clash = await client.put(f"/api/photos/people/{known['bea']}",
                                     json={"name": "Ann Test"})
            renamed = await client.put(f"/api/photos/people/{known['bea']}",
                                       json={"given_name": "Beatrice"})
            dated = await client.put(f"/api/photos/people/{known['bea']}",
                                     json={"born_on": "2021-01-01"})
        async with db.SessionLocal() as session:
            await session.execute(Person.__table__.update().where(Person.id == known["bea"])
                                  .values(born_source="contacts"))
            await session.commit()
        async with library(await signed_in("boss2")) as client:
            booked = await client.put(f"/api/photos/people/{known['bea']}",
                                      json={"born_on": "2000-01-01"})
        return groups, clash, renamed, dated, booked, await _state(await _faces(scene, "b2020"))

    groups, clash, renamed, dated, booked, b2020 = run(scenario())
    assert clash.status_code == 409
    assert renamed.json()["name"] == "Beatrice Test"
    assert dated.status_code == 200
    assert [h["cluster"] for h in dated.json()["handed_back"]] == [groups["b2020"]]
    assert dated.json()["born_source"] == "typed"
    assert {person for _, person, _ in b2020} == {None}
    assert booked.status_code == 409


def test_a_group_is_given_by_name_and_refused_before_a_birth(household):
    scene, known, reels = household

    async def scenario():
        groups = await _groups(scene)
        async with db.SessionLocal() as session:
            await session.execute(Person.__table__.update().where(Person.id == known["bea"])
                                  .values(born_on=datetime.date(2021, 1, 1)))
            await session.commit()
        async with library(await signed_in("boss")) as client:
            named = await client.put(f"/api/photos/clusters/{groups['a2021']}/person",
                                     json={"name": "Cora Test"})
            refused = await client.put(f"/api/photos/clusters/{groups['a2020']}/person",
                                       json={"person_id": known["bea"]})
            overruled = await client.put(f"/api/photos/clusters/{groups['a2020']}/person",
                                         json={"person_id": known["bea"], "ignore_dates": True})
            nobody = await client.put(f"/api/photos/clusters/{groups['a2020']}/person", json={})
            missing = await client.put("/api/photos/clusters/999999/person", json={"name": "X"})
            taken_off = await client.delete(f"/api/photos/clusters/{groups['a2021']}/person")
        return (groups, named, refused, overruled, nobody, missing, taken_off,
                await _state(await _faces(scene, "a2021")))

    groups, named, refused, overruled, nobody, missing, taken_off, a2021 = run(scenario())
    assert named.status_code == 200
    assert named.json()["person"]["name"] == "Cora Test"
    assert named.json()["years"] == [2021, 2021]
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "before they were born"
    assert refused.json()["detail"]["earliest_photograph"].startswith("2020-")
    assert overruled.status_code == 200
    assert nobody.status_code == 422
    assert missing.status_code == 404
    assert taken_off.json() == {"cluster": groups["a2021"], "person": None}
    assert {person for _, person, _ in a2021} == {None}


def test_single_faces_are_given_taken_off_and_thrown_away(household):
    scene, known, reels = household

    async def scenario():
        a2021 = await _faces(scene, "a2021")
        reel = _reel(reels, known["bea"])
        async with library(await signed_in("boss")) as client:
            given = await client.post("/api/photos/faces/person",
                                      json={"faces": a2021[:3], "person_id": known["bea"]})
            loosened = await client.post("/api/photos/faces/detach", json={"faces": a2021[:2]})
            thrown = await client.post("/api/photos/faces/discard", json={"faces": a2021[3:5]})
            nothing = await client.post("/api/photos/faces/discard", json={"faces": []})
        return a2021, reel, given, loosened, thrown, nothing, await _state(a2021)

    a2021, reel, given, loosened, thrown, nothing, state = run(scenario())
    assert given.json()["faces"] == 3 and given.json()["person"]["id"] == known["bea"]
    assert given.json()["years"] == [2021, 2021]
    assert given.json()["reels"] == 1 and not reel.exists()
    assert loosened.json() == {"faces": 2, "reels": 0}
    assert thrown.json() == {"faces": 2, "reels": 0}
    assert nothing.json() == {"faces": 0}
    by_id = {face: (person, group) for face, person, group in state}
    assert by_id[a2021[0]] == (None, None)
    assert by_id[a2021[2]] == (known["bea"], None)
    assert a2021[3] not in by_id and a2021[4] not in by_id
    assert all(by_id[face][1] is not None for face in a2021[5:])


def test_groups_are_taken_off_and_thrown_away_in_bulk(household):
    scene, known, reels = household

    async def scenario():
        groups = await _groups(scene)
        reel = _reel(reels, known["bea"])
        async with library(await signed_in("boss")) as client:
            loosened = await client.post("/api/photos/clusters/detach",
                                         json={"clusters": [groups["b2022"]]})
            thrown = await client.post("/api/photos/clusters/discard",
                                       json={"clusters": [groups["a2021"], groups["undated0"]]})
            one = await client.delete(f"/api/photos/clusters/{groups['undated1']}")
            gone = await client.delete(f"/api/photos/clusters/{groups['undated1']}")
        async with db.SessionLocal() as session:
            left = set((await session.execute(select(FaceCluster.id))).scalars())
            faces = await session.scalar(select(func.count()).select_from(Face))
        return groups, reel, loosened, thrown, one, gone, left, faces, \
            await _state(await _faces(scene, "b2022"))

    groups, reel, loosened, thrown, one, gone, left, faces, b2022 = run(scenario())
    assert loosened.json() == {"clusters": 1, "faces": 9, "reels": 1}
    assert not reel.exists()
    assert {person for _, person, _ in b2022} == {None}
    assert thrown.json() == {"clusters": 2, "faces": 10, "reels": 0}
    assert one.json() == {"cluster": groups["undated1"], "faces": 1}
    assert gone.status_code == 404
    assert not {groups["a2021"], groups["undated0"], groups["undated1"]} & left
    assert faces == len(scene.labels) - 11


def test_the_runs_are_written_down_and_a_life_is_named_whole(household):
    scene, known, reels = household

    async def scenario():
        groups = await _groups(scene)
        async with db.SessionLocal() as session:
            await session.execute(FaceCluster.__table__.update()
                                  .where(FaceCluster.id == groups["b2022"]).values(person_id=None))
            await session.execute(Face.__table__.update()
                                  .where(Face.cluster_id == groups["b2022"]).values(person_id=None))
            await session.commit()
        async with library(await signed_in("boss")) as client:
            suggested = await client.get("/api/photos/people/suggested")
            adopted = await client.post("/api/photos/people/lives/adopt")
            named = await client.put("/api/photos/people/lives", json={
                "clusters": [groups["a2020"], groups["a2021"]], "name": "Cora Test",
                "born_on": "2021-01-01"})
        return groups, suggested, adopted, named

    groups, suggested, adopted, named = run(scenario())
    assert suggested.status_code == 200 and suggested.json()["total"] >= 1
    assert adopted.json()["refused"] == []
    assert named.json()["person"]["name"] == "Cora Test"
    assert named.json()["given"] == [groups["a2021"]]
    assert [r["cluster"] for r in named.json()["refused"]] == [groups["a2020"]]


def test_the_leanings_of_a_group_name_who_each_face_is_nearer(household):
    scene, known, reels = household

    async def scenario():
        groups = await _groups(scene)
        async with library(await signed_in("boss")) as client:
            return await client.get(f"/api/photos/clusters/{groups['a2020']}/leanings")

    said = run(scenario()).json()
    assert set(said) == {"leanings", "people"}
    for leaning in said["leanings"]:
        assert set(leaning) == {"face", "person_id", "name", "sim", "margin"}
        assert leaning["margin"] >= 0.05
    assert sum(p["faces"] for p in said["people"]) == len(said["leanings"])
