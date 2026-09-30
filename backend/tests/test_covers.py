import datetime

from sqlalchemy import select, update

from conftest import library, run, signed_in
from opus import db
from opus.models import Face, Person, Photo
from opus.photos.people import covers
from scene import Scene


def when(year: int) -> datetime.datetime:
    return datetime.datetime(year, 6, 1, tzinfo=datetime.UTC)


async def _household():
    """Kata: one sharp, confident face at eleven; a recent year of ten faces, one
    of them large and typical, one large and turned away, the rest small; and a
    later year with too few faces to count."""
    scene = Scene()
    kata = scene.base()
    faces = {}
    async with db.SessionLocal() as session:
        person = Person(name="Kata Babić", given_name="Kata", family_name="Babić")
        session.add(person)
        await session.flush()

        async def face(year, width_px, vector, score=0.9):
            photo = await scene.photo(session, when(year))
            # the scene's face is a fifth of the picture across
            await session.execute(update(Photo).where(Photo.id == photo)
                                  .values(pixel_w=width_px * 5, pixel_h=width_px * 5))
            made = await scene.face(session, photo, vector, "kata", person_id=person.id)
            await session.execute(update(Face).where(Face.id == made).values(score=score))
            return made

        faces["child"] = await face(2012, 900, scene.sample(kata, 0.05), score=0.99)
        for _ in range(8):
            await face(2012, 150, scene.sample(kata, 0.05))
        faces["typical"] = await face(2025, 450, scene.sample(kata, 0.02), score=0.85)
        faces["turned"] = await face(2025, 600, scene.turned(kata, 0.4).tolist(), score=0.95)
        for _ in range(8):
            await face(2025, 120, scene.sample(kata, 0.01))
        faces["too_few"] = await face(2026, 800, scene.sample(kata, 0.01))
        await session.commit()
    return person.id, faces


def test_the_face_on_the_wall_is_a_recent_large_typical_one(clean):
    async def scenario():
        person, faces = await _household()
        first = await covers.run()
        again = await covers.run()
        async with db.SessionLocal() as session:
            kept = (await session.execute(select(Person.cover_face_id).where(Person.id == person))).scalar()
        cookie = await signed_in("boss")
        async with library(cookie) as client:
            listed = (await client.get("/api/photos/people")).json()
        return faces, first, again, kept, listed

    faces, first, again, kept, listed = run(scenario())
    assert kept == faces["typical"]
    assert (first, again) == ({"chosen": 1}, {"chosen": 0})
    assert listed[0]["cover"] == faces["typical"]


def test_a_cover_that_is_somebody_elses_now_is_chosen_again(clean):
    async def scenario():
        person, faces = await _household()
        await covers.run()
        async with db.SessionLocal() as session:
            other = Person(name="Jana Babić", given_name="Jana", family_name="Babić")
            session.add(other)
            await session.flush()
            await session.execute(update(Face).where(Face.id == faces["typical"]).values(person_id=other.id))
            await session.commit()
        await covers.run()
        async with db.SessionLocal() as session:
            return faces, (await session.execute(
                select(Person.cover_face_id).where(Person.id == person))).scalar()

    faces, kept = run(scenario())
    assert kept not in (faces["typical"], None)
    assert kept != faces["child"]
