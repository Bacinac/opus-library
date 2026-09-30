import datetime
import hashlib

import numpy as np
from sqlalchemy.dialects.postgresql import insert

from opus import db
from opus.models import FACE_DIMS, FACE_GENERATION, Face, FaceCluster, Person, Photo, Setting

TIGHT = 0.15
LOOSE = 3.0


class Scene:
    """A household photographed over three years: two people, one of them named,
    one of them split into two islands inside a year, one stray face, one blurred
    face and two undated photographs."""

    def __init__(self, seed: int = 7):
        self.rng = np.random.default_rng(seed)
        self.photos = 0
        self.labels: dict[int, str] = {}

    def unit(self, v):
        return v / np.linalg.norm(v)

    def base(self):
        return self.unit(self.rng.normal(size=FACE_DIMS))

    def turned(self, v, cos: float):
        r = self.rng.normal(size=FACE_DIMS)
        r = self.unit(r - r.dot(v) * v)
        return self.unit(cos * v + (1 - cos ** 2) ** 0.5 * r)

    def sample(self, v, spread: float):
        noise = self.rng.normal(size=FACE_DIMS)
        return self.unit(v + noise / np.linalg.norm(noise) * spread ** 0.5).tolist()

    async def photo(self, session, when: datetime.datetime | None) -> int:
        self.photos += 1
        row = Photo(checksum=hashlib.sha1(str(self.photos).encode()).digest(),
                    taken_at=when, kind="image")
        session.add(row)
        await session.flush()
        return row.id

    async def face(self, session, photo_id: int, vector, label: str, **extra) -> int:
        row = Face(photo_id=photo_id, x=0.1, y=0.1, w=0.2, h=0.2, score=0.9,
                   embedding=vector, generation=FACE_GENERATION,
                   sharpness=extra.pop("sharpness", 1.0), **extra)
        session.add(row)
        await session.flush()
        self.labels[row.id] = label
        return row.id

    async def build(self, ann: bool = False) -> dict:
        a1 = self.base()
        a2 = self.turned(a1, 0.5)
        b = self.base()
        in_2020 = datetime.datetime(2020, 6, 1, tzinfo=datetime.UTC)
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(key="faces_min_sharpness", value="0.2"))
            bea = Person(name="Bea Test", given_name="Bea", family_name="Test")
            session.add(bea)
            named = Person(name="Ann Test", given_name="Ann", family_name="Test")
            session.add(named)
            stale = FaceCluster(generation=FACE_GENERATION)
            session.add(stale)
            await session.flush()

            for day in range(10):
                pid = await self.photo(session, in_2020 + datetime.timedelta(days=day))
                await self.face(session, pid, self.sample(a1, TIGHT), "a2020",
                                cluster_id=stale.id)
            for day in range(10):
                pid = await self.photo(session, in_2020 + datetime.timedelta(days=20 + day))
                await self.face(session, pid, self.sample(a2, TIGHT), "a2020",
                                person_id=named.id if ann else None)
            for day in range(10):
                pid = await self.photo(session, in_2020 + datetime.timedelta(days=40 + day))
                await self.face(session, pid, self.sample(b, TIGHT), "b2020",
                                person_id=bea.id if day == 0 else None)
                if day < 3:
                    await self.face(session, pid, self.sample(a1, TIGHT), "a2020")
            pid = await self.photo(session, in_2020 + datetime.timedelta(days=60))
            await self.face(session, pid, self.sample(a1, LOOSE), "a2020")
            pid = await self.photo(session, in_2020 + datetime.timedelta(days=61))
            await self.face(session, pid, self.sample(a1, TIGHT), "blurred",
                            sharpness=0.1, cluster_id=stale.id)
            for day in range(9):
                pid = await self.photo(session, datetime.datetime(2021, 3, 1 + day, tzinfo=datetime.UTC))
                await self.face(session, pid, self.sample(a1, TIGHT), "a2021")
            for day in range(9):
                pid = await self.photo(session, datetime.datetime(2022, 3, 1 + day, tzinfo=datetime.UTC))
                await self.face(session, pid, self.sample(b, TIGHT), "b2022")
            for n in range(2):
                pid = await self.photo(session, None)
                await self.face(session, pid, self.sample(a1, TIGHT), f"undated{n}")
            await session.commit()
            return {"bea": bea.id, "ann": named.id}
