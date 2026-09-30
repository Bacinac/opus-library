import asyncio

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from conftest import library, run, signed_in
from opus import db
from opus.models import Face, FaceCluster, Photo, Setting
from opus.photos.people import cluster, lives
from scene import Scene


async def _grouped(scene: Scene):
    async with db.SessionLocal() as session:
        faces = (await session.execute(
            select(Face.id, Face.cluster_id, Face.person_id))).all()
        owners = dict((await session.execute(
            select(FaceCluster.id, FaceCluster.person_id))).all())
    groups: dict[int | None, set[str]] = {}
    people: dict[str, set[int | None]] = {}
    for face_id, cluster_id, person_id in faces:
        label = scene.labels[face_id]
        groups.setdefault(cluster_id, set()).add(label)
        people.setdefault(label, set()).add(person_id)
    named = {frozenset(labels): owners.get(cid) for cid, labels in groups.items() if cid}
    return groups, people, named


def test_regrouping_the_household(clean):
    scene = Scene()
    known = run(scene.build())
    said = run(cluster.run())
    assert said == {"clusters": 6, "kept_names": 1, "adopted": 1, "welded": 1, "named": 1}

    groups, people, named = run(_grouped(scene))
    assert groups.pop(None) == {"blurred"}
    assert sorted(sorted(labels) for labels in groups.values()) == [
        ["a2020"], ["a2021"], ["b2020"], ["b2022"], ["undated0"], ["undated1"]]
    assert named[frozenset({"b2020"})] == known["bea"]
    assert named[frozenset({"b2022"})] == known["bea"]
    assert named[frozenset({"a2020"})] is None
    assert people["b2022"] == {known["bea"]}
    assert people["b2020"] == {known["bea"], None}
    assert people["a2020"] == {None}


def test_a_life_across_the_years(clean):
    scene = Scene()
    known = run(scene.build())
    run(cluster.run())

    async def ask():
        async with db.SessionLocal() as session:
            return await lives.runs(session)

    found = run(ask())
    assert [life["person"] for life in found] == [{"id": known["bea"], "name": "Bea Test"}, None]
    assert [[era["year"] for era in life["groups"]] for life in found] == [[2020, 2022], [2020, 2021]]
    assert [life["faces"] for life in found] == [19, 33]
    assert [life["years"] for life in found] == [[2020, 2022], [2020, 2021]]
    assert all(life["born"] is None and not life["certain"] for life in found)


def test_a_welded_island_carries_its_name(clean):
    scene = Scene()
    known = run(scene.build(ann=True))
    said = run(cluster.run())
    assert said == {"clusters": 6, "kept_names": 2, "adopted": 1, "welded": 1, "named": 2}

    groups, people, named = run(_grouped(scene))
    assert named[frozenset({"a2020"})] == known["ann"]
    assert named[frozenset({"a2021"})] == known["ann"]
    assert people["a2021"] == {known["ann"]}
    assert people["a2020"] == {known["ann"], None}

    async def ask():
        async with db.SessionLocal() as session:
            return await lives.runs(session)

    found = run(ask())
    assert [life["person"]["name"] for life in found] == ["Ann Test", "Bea Test"]
    assert [[era["year"] for era in life["groups"]] for life in found] == [[2020, 2021], [2020, 2022]]


def test_a_name_given_during_a_regrouping_is_refused_not_lost(quick):
    scene = Scene()
    run(scene.build())
    run(cluster.run())

    async def scenario():
        cookie = await signed_in("boss")
        async with db.SessionLocal() as session:
            target = await session.scalar(
                select(Face.cluster_id).where(Face.cluster_id.is_not(None)).limit(1))
        async with library(cookie) as client, db.SessionLocal() as regrouping:
            await regrouping.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                     {"key": cluster.REGROUPING})
            refused = await client.put(f"/api/photos/clusters/{target}/person",
                                       json={"name": "Cora Test"})
            dropped = await client.post("/api/photos/clusters/discard",
                                        json={"clusters": [target]})
            await regrouping.rollback()
            given = await client.put(f"/api/photos/clusters/{target}/person",
                                     json={"name": "Cora Test"})
        async with db.SessionLocal() as session:
            owners = set((await session.execute(
                select(Face.person_id).where(Face.cluster_id == target))).scalars())
        return refused, dropped, given, owners

    refused, dropped, given, owners = run(scenario())
    assert (refused.status_code, refused.json()) == (409, {"detail": {"reason": "regrouping"}})
    assert (dropped.status_code, dropped.json()) == (409, {"detail": {"reason": "regrouping"}})
    assert given.status_code == 200
    assert owners == {given.json()["person"]["id"]}


def test_a_regrouping_waits_for_a_change_in_flight(clean):
    scene = Scene()
    run(scene.build())

    async def scenario():
        async with db.SessionLocal() as naming:
            await cluster.unmoved(naming)
            regrouping = asyncio.ensure_future(cluster.run())
            await asyncio.sleep(1)
            waited = not regrouping.done()
            await naming.commit()
        said = await regrouping
        return waited, said

    waited, said = run(scenario())
    assert waited
    assert said["clusters"] == 6


def test_a_page_of_groups_is_bounded(quick):
    async def scenario():
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            return [(await client.get("/api/photos/clusters", params=params)).status_code
                    for params in ({"after": -1}, {"limit": 301}, {"min_size": 0},
                                   {"limit": 300, "after": 0})]

    assert run(scenario()) == [422, 422, 422, 200]


def test_forgetting_a_photograph_or_the_smears_waits_for_no_regrouping(quick, tmp_path):
    scene = Scene()
    run(scene.build())
    run(cluster.run())

    async def scenario():
        cookie = await signed_in("boss")
        async with db.SessionLocal() as session:
            for key, value in (("photos_dir", str(tmp_path)), ("photos_write_dir", str(tmp_path)),
                               ("photos_derivatives_dir", str(tmp_path / "derivatives"))):
                await session.execute(insert(Setting).values(key=key, value=value))
            await session.commit()
            checksum = (await session.scalar(
                select(Photo.checksum).join(Face, Face.photo_id == Photo.id)
                .where(Face.cluster_id.is_not(None)).limit(1))).hex()
            faces = await session.scalar(select(func.count()).select_from(Face))
        async with library(cookie) as client, db.SessionLocal() as regrouping:
            await regrouping.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                     {"key": cluster.REGROUPING})
            photo = await client.delete(f"/api/photos/{checksum}")
            smears = await client.delete("/api/photos/library/focus")
            await regrouping.rollback()
        async with db.SessionLocal() as session:
            left = await session.scalar(select(func.count()).select_from(Face))
            kept = await session.scalar(
                select(func.count()).select_from(Photo)
                .where(Photo.checksum == bytes.fromhex(checksum)))
        return photo, smears, faces, left, kept

    photo, smears, faces, left, kept = run(scenario())
    assert (photo.status_code, photo.json()) == (409, {"detail": {"reason": "regrouping"}})
    assert (smears.status_code, smears.json()) == (409, {"detail": {"reason": "regrouping"}})
    assert (left, kept) == (faces, 1)
