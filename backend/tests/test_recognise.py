import datetime
import hashlib

import numpy as np
import pytest
from sqlalchemy import select

from conftest import run
from opus import db
from opus.models import COMPARED, FACE_DIMS, FACE_GENERATION, Face, FaceCluster, Person, Photo
from opus.photos.people import recognise


def alike(sim: float, axis: int) -> list[float]:
    vector = np.zeros(FACE_DIMS)
    vector[0] = sim
    vector[axis] = (1 - sim ** 2) ** 0.5
    return vector.tolist()


class House:
    def __init__(self):
        self.photos = 0
        self.axes = 1
        self.people: dict[str, int] = {}
        self.groups: dict[str, int] = {}

    async def person(self, session, name: str, born_on: datetime.date | None = None) -> None:
        row = Person(name=name, born_on=born_on)
        session.add(row)
        await session.flush()
        self.people[name] = row.id

    async def group(self, session, label: str, years: list[int | None], *, sim: float = 1.0,
                    person: str | None = None, faces_at: int = COMPARED) -> None:
        self.axes += 1
        vector = alike(sim, self.axes)
        owner = self.people.get(person)
        cluster = FaceCluster(person_id=owner, centroid=vector, faces_at=faces_at,
                              generation=FACE_GENERATION)
        session.add(cluster)
        await session.flush()
        for year in years:
            self.photos += 1
            photo = Photo(checksum=hashlib.sha1(str(self.photos).encode()).digest(),
                          taken_at=datetime.datetime(year, 6, 1, tzinfo=datetime.UTC) if year else None)
            session.add(photo)
            await session.flush()
            session.add(Face(photo_id=photo.id, x=0.1, y=0.1, w=0.2, h=0.2, score=0.9,
                             embedding=vector, generation=FACE_GENERATION, sharpness=1.0,
                             cluster_id=cluster.id, person_id=owner))
        await session.flush()
        self.groups[label] = cluster.id


def recognised(build) -> tuple[dict, dict[str, int | None], dict[str, set[int | None]]]:
    house = House()

    async def go():
        async with db.SessionLocal() as session:
            await build(session, house)
            await session.commit()
            said = await recognise.run(session)
            await session.commit()
            owners = dict((await session.execute(
                select(FaceCluster.id, FaceCluster.person_id))).all())
            faces: dict[int, set[int | None]] = {}
            for cluster_id, person_id in (await session.execute(
                    select(Face.cluster_id, Face.person_id))).all():
                faces.setdefault(cluster_id, set()).add(person_id)
        names = {pid: name for name, pid in house.people.items()}
        return (said,
                {label: names.get(owners[cid]) for label, cid in house.groups.items()},
                {label: {names.get(p) for p in faces[cid]} for label, cid in house.groups.items()})

    return run(go())


def test_a_group_takes_the_name_of_the_one_person_clearly_nearest(clean):
    async def build(session, house):
        await house.person(session, "Ann")
        await house.person(session, "Bea")
        await house.group(session, "new", [2020, 2020])
        await house.group(session, "ann", [2019], sim=0.80, person="Ann")
        await house.group(session, "bea", [2020], sim=0.50, person="Bea")

    said, owners, faces = recognised(build)
    assert said == {"considered": 1, "named": 1, "unsure": 0}
    assert owners == {"new": "Ann", "ann": "Ann", "bea": "Bea"}
    assert faces["new"] == {"Ann"}


@pytest.mark.parametrize(("year", "named"), [
    (2020 - recognise.WITHIN_YEARS, "Ann"), (2020 + recognise.WITHIN_YEARS, "Ann"),
    (2020 - recognise.WITHIN_YEARS - 1, None), (2020 + recognise.WITHIN_YEARS + 1, None)])
def test_a_group_is_offered_to_faces_three_years_either_side_and_no_further(clean, year, named):
    async def build(session, house):
        await house.person(session, "Ann")
        await house.group(session, "new", [2020])
        await house.group(session, "ann", [year], sim=0.90, person="Ann")

    said, owners, _ = recognised(build)
    assert owners["new"] == named
    assert said["considered"] == (1 if named else 0)


def test_a_likeness_below_known_is_not_offered(clean):
    async def build(session, house):
        await house.person(session, "Ann")
        await house.group(session, "new", [2020])
        await house.group(session, "ann", [2020], sim=recognise.KNOWN - 0.04, person="Ann")

    said, owners, _ = recognised(build)
    assert said == {"considered": 0, "named": 0, "unsure": 0}
    assert owners["new"] is None


def test_two_people_nearly_as_near_leave_the_group_nameless(clean):
    async def build(session, house):
        await house.person(session, "Ann")
        await house.person(session, "Bea")
        await house.group(session, "new", [2020])
        await house.group(session, "ann", [2020], sim=0.80, person="Ann")
        await house.group(session, "bea", [2021], sim=0.80 - recognise.CLEARLY + 0.03, person="Bea")

    said, owners, faces = recognised(build)
    assert said == {"considered": 1, "named": 0, "unsure": 1}
    assert owners["new"] is None and faces["new"] == {None}


def test_a_rival_clearly_further_away_does_not_stop_the_name(clean):
    async def build(session, house):
        await house.person(session, "Ann")
        await house.person(session, "Bea")
        await house.group(session, "new", [2020])
        await house.group(session, "ann", [2020], sim=0.80, person="Ann")
        await house.group(session, "bea", [2020], sim=0.80 - recognise.CLEARLY - 0.03, person="Bea")

    said, owners, _ = recognised(build)
    assert said == {"considered": 1, "named": 1, "unsure": 0}
    assert owners["new"] == "Ann"


def test_a_rival_photographed_only_outside_the_years_is_no_rival(clean):
    async def build(session, house):
        await house.person(session, "Ann")
        await house.person(session, "Bea")
        await house.group(session, "new", [2020])
        await house.group(session, "ann", [2020], sim=0.80, person="Ann")
        await house.group(session, "bea", [2010], sim=0.79, person="Bea")

    said, owners, _ = recognised(build)
    assert said == {"considered": 1, "named": 1, "unsure": 0}
    assert owners["new"] == "Ann"


def test_nobody_is_given_a_group_photographed_before_they_were_born(clean):
    async def build(session, house):
        await house.person(session, "Ann", born_on=datetime.date(2021, 1, 1))
        await house.group(session, "new", [2020])
        await house.group(session, "ann", [2022], sim=0.90, person="Ann")

    said, owners, _ = recognised(build)
    assert said["considered"] == 0 and owners["new"] is None


def test_a_sister_not_yet_born_is_no_rival_to_the_one_who_was(clean):
    async def build(session, house):
        await house.person(session, "Ann", born_on=datetime.date(2017, 3, 1))
        await house.person(session, "Bea", born_on=datetime.date(2021, 1, 1))
        await house.group(session, "new", [2020])
        await house.group(session, "ann", [2019], sim=0.80, person="Ann")
        await house.group(session, "bea", [2022], sim=0.85, person="Bea")

    said, owners, _ = recognised(build)
    assert said == {"considered": 1, "named": 1, "unsure": 0}
    assert owners["new"] == "Ann"


def test_only_nameless_groups_with_enough_faces_and_a_date_are_asked(clean):
    async def build(session, house):
        await house.person(session, "Ann")
        await house.group(session, "ann", [2020], sim=0.90, person="Ann")
        await house.group(session, "thin", [2020], sim=1.0, faces_at=COMPARED - 1)
        await house.group(session, "undated", [None], sim=1.0)

    said, owners, _ = recognised(build)
    assert said == {"considered": 0, "named": 0, "unsure": 0}
    assert owners["thin"] is None and owners["undated"] is None
