"""The one face that stands for somebody on a wall of people.

It was the detector's most confident face of them, from any year: Kata at eleven,
71 pixels across, on a shelf where she is twenty-four. Measured on the five
busiest people here (2026-09-17), what reads as them now is a face from the
latest year holding at least ten of theirs, at least 200 pixels across, and the
nearest of those to their own centre across every year — which sets aside a
wink, a grimace and a face half turned away. A year's own centre was tried and
is worse: a summer of holiday photographs makes the squint the typical face.

Chosen in the photo loop and kept on the person, because the centre of five
thousand faces is not something to average for every person every time the
wall is opened. It is chosen again when the face stops being theirs or a later
year gathers enough of them."""

from sqlalchemy import text

from opus.db import SessionLocal

RECENT_FACES = 10
LEAST_PX = 200
LEAST_SCORE = 0.8

_CHOSEN = text("""
WITH mine AS (
    SELECT f.id, f.embedding, f.score, f.w * ph.pixel_w AS px,
           EXTRACT(YEAR FROM ph.taken_at)::int AS yr
    FROM photo_faces f JOIN photos ph ON ph.id = f.photo_id
    WHERE f.person_id = :person AND f.at_seconds IS NULL
), recent AS (
    SELECT COALESCE(
        (SELECT yr FROM mine WHERE yr IS NOT NULL GROUP BY yr
          HAVING count(*) >= :recent ORDER BY yr DESC LIMIT 1),
        (SELECT max(yr) FROM mine)) AS yr
), centre AS (
    SELECT avg(embedding) AS centre FROM mine
)
SELECT m.id FROM mine m
ORDER BY m.yr IS NOT DISTINCT FROM (SELECT yr FROM recent) DESC,
         COALESCE(m.px, 0) >= :least_px DESC,
         m.score >= :least_score DESC,
         m.embedding <=> (SELECT centre FROM centre)
LIMIT 1
""")

# a person whose cover is gone, is somebody else's now, or is older than the
# latest year that has gathered enough of their faces
_STALE = text("""
WITH years AS (
    SELECT f.person_id, EXTRACT(YEAR FROM ph.taken_at)::int AS yr
    FROM photo_faces f JOIN photos ph ON ph.id = f.photo_id
    WHERE f.person_id IS NOT NULL AND f.at_seconds IS NULL AND ph.taken_at IS NOT NULL
    GROUP BY f.person_id, yr HAVING count(*) >= :recent
), latest AS (
    SELECT person_id, max(yr) AS yr FROM years GROUP BY person_id
)
SELECT p.id FROM photo_people p
LEFT JOIN photo_faces c ON c.id = p.cover_face_id
LEFT JOIN photos cp ON cp.id = c.photo_id
LEFT JOIN latest l ON l.person_id = p.id
WHERE EXISTS (SELECT 1 FROM photo_faces f WHERE f.person_id = p.id)
  AND (c.id IS NULL OR c.person_id IS DISTINCT FROM p.id
       OR (l.yr IS NOT NULL AND EXTRACT(YEAR FROM cp.taken_at)::int IS DISTINCT FROM l.yr))
""")


async def choose(session, person_id: int) -> int | None:
    return (await session.execute(_CHOSEN, {
        "person": person_id, "recent": RECENT_FACES,
        "least_px": LEAST_PX, "least_score": LEAST_SCORE})).scalar()


async def run() -> dict:
    chosen = 0
    async with SessionLocal() as session:
        stale = (await session.execute(_STALE, {"recent": RECENT_FACES})).scalars().all()
        for person_id in stale:
            face = await choose(session, person_id)
            await session.execute(text("UPDATE photo_people SET cover_face_id = :face WHERE id = :person"),
                                  {"face": face, "person": person_id})
            chosen += 1
        await session.commit()
    return {"chosen": chosen}
