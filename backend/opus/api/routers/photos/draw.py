"""Photographs drawn at random, for whoever wants to ask a question about one.

The shelf next door is the archive in the order it happened; this is the same
archive with the order taken away. What it draws is not "some photographs" but
photographs that are known to carry a particular fact — who is in them, where
they were taken, when — because a question can only be asked of a picture whose
answer the library actually holds.

The drawing happens here and not in the caller. Which of forty thousand rows
know a fact is a question about the catalogue, and a caller that fetched pages
until it had enough would be reading the whole library to play one round."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from opus.db import get_session
from opus.photos.library import derive
from opus.photos.people.portraits import crop_square

router = APIRouter()

# What each draw insists on. `person` wants a photograph of exactly one named
# person: two of them and the picture has two right answers, and nobody named
# and it has none. `place` and `date` want the fact itself, and a date is
# trusted only when a camera wrote it — a date read off a filename is a guess,
# and a guess is not something to be scored against.
# The detector's confidence that what it found is a face, and the nearest thing
# the catalogue has to how hard somebody is to recognise: a low one is a face
# that is small, turned away, half behind somebody, or lost in the light.
# Measured across every named face here — 0.50 at worst, 0.83 in the middle,
# 0.94 at best — so this is the harder half rather than a number picked to
# sound careful. It leaves 7,691 photographs of the 13,147 with exactly one
# person in them, which is a deep enough well for a round of ten.
#
# Not lower than this on purpose. Below about 0.71 the face is often barely
# there, and a question nobody could answer from memory is not a harder
# question — it is a coin.
RECOGNISABLE = 0.831

# What a caller may ask for. The top is every named face there is; the floor is
# not the worst detection in the archive but the lowest one still worth being
# asked about — beneath roughly this the face is barely there.
EASIEST = 0.938
HARDEST = 0.700

KNOWS = {
    "person": """
        p.id IN (SELECT f.photo_id FROM photo_faces f
                  WHERE f.person_id IS NOT NULL
                  GROUP BY f.photo_id
                 HAVING count(DISTINCT f.person_id) = 1
                    AND max(f.score) < :hard)
    """,
    "place": "p.place <> ''",
    "date": "p.taken_source = 'exif'",
}


@router.get("/draw")
async def draw(knows: str = Query("date", pattern="^(person|place|date)$"),
               hard: float = Query(RECOGNISABLE, ge=HARDEST, le=EASIEST),
               count: int = Query(12, ge=1, le=60),
               session=Depends(get_session)):
    """Photographs that know the named fact, in no order at all.

    Every fact the picture holds comes back with it, not only the one asked
    for: a round that asks where this was taken reveals the day and the people
    along with the answer, and asking again for what was already on the row
    would be a second request for a photograph already in hand.

    Only pictures with a derivative are drawn. A question is a picture on a
    screen, and one that has not been through the pass yet is a grey box."""
    rows = (await session.execute(text(f"""
        SELECT p.id, encode(p.checksum, 'hex'), p.taken_at, p.taken_source,
               p.place, p.country, p.pixel_w, p.pixel_h, p.thumbhash, p.turn
        FROM photos p
        WHERE p.derived_gen = :gen AND p.kind = 'image'
          AND p.taken_at IS NOT NULL
          AND ({KNOWS[knows]})
        ORDER BY random()
        LIMIT :count
    """), {"gen": derive.GENERATION, "count": count,
           "hard": hard})).all()
    if not rows:
        return {"photos": []}

    # Who is in them, asked of the drawn dozen rather than joined into the draw
    # itself. Joined, a photograph with four faces is four rows and the LIMIT
    # counts faces instead of pictures.
    faces = (await session.execute(text("""
        SELECT f.photo_id, f.person_id, pe.name, pe.born_on,
               f.x, f.y, f.w, f.h, f.apparent_age, f.id
        FROM photo_faces f JOIN photo_people pe ON pe.id = f.person_id
        WHERE f.photo_id = ANY(:ids) AND f.person_id IS NOT NULL
        ORDER BY f.score DESC
    """), {"ids": [r[0] for r in rows]})).all()
    sized = {r[0]: (r[6], r[7]) for r in rows}
    who: dict[int, list[dict]] = {}
    for pid, person, name, born, x, y, w, h, age, face in faces:
        # the best face of each person on the picture, and only that one: the
        # same person twice is the same answer twice
        theirs = who.setdefault(pid, [])
        if any(one["id"] == person for one in theirs):
            continue
        theirs.append({
            "id": person, "name": name,
            # the face itself, so a question about who this is can be asked with
            # the face rather than with the photograph it was found in
            "face": face,
            # and the rectangle that face is CUT OUT as, worked out here rather
            # than by whoever draws it: what was shown and what is framed on the
            # whole photograph afterwards have to be the same rectangle
            "frame": _frame(x, y, w, h, *sized.get(pid, (None, None))),
            "born_on": born.isoformat() if born else None,
            "box": {"x": x, "y": y, "w": w, "h": h},
            # how old they LOOK here, which is what tells a picture of a child
            # apart from one of the same person grown up
            "age": age,
        })

    if knows == "person":
        await _lookalikes(session, who)

    return {"photos": [{
        "id": checksum,
        "taken_at": taken.isoformat(),
        "dated": source,
        "place": place or None,
        "country": country or None,
        "w": pw, "h": ph,
        "hash": thumb.hex() if thumb else None,
        "turn": turn,
        "people": who.get(pid, []),
    } for pid, checksum, taken, source, place, country, pw, ph, thumb, turn in rows]}


def _frame(x: float, y: float, w: float, h: float,
           width: int | None, height: int | None) -> dict | None:
    """Where that crop sits on the whole photograph, as fractions of it.

    Fractions rather than pixels because the picture is drawn at whatever size
    the screen has, and the preview it was cut from is a third size again. The
    rectangle itself is the crop endpoint's, asked of it rather than repeated."""
    if not width or not height:
        return None
    left, top, side = crop_square(x, y, w, h, width, height)
    return {"x": left / width, "y": top / height,
            "w": side / width, "h": side / height}


async def _lookalikes(session, who: dict[int, list[dict]]) -> None:
    """Who else the archive thinks looks like this, nearest first.

    The wrong answers are the game. Three names drawn at random are three names
    somebody rules out without looking at the face — the ones worth offering are
    the people this face is actually near in the space the library recognises
    everybody in.

    Asked of the groups' centroids and not of every face: forty-one thousand
    faces is half a second of arithmetic, and the same question against the
    indexed centroids is half a millisecond."""
    faces = [(pid, theirs[0]["face"]) for pid, theirs in who.items() if theirs]
    if not faces:
        return
    rows = (await session.execute(text("""
        SELECT f.id, near.person_id, pe.name, near.d
        FROM photo_faces f
        CROSS JOIN LATERAL (
            SELECT c.person_id, c.centroid <=> f.embedding AS d
            FROM photo_face_clusters c
            WHERE c.centroid IS NOT NULL AND c.person_id IS NOT NULL
              -- and not their own groups. Somebody with four thousand faces
              -- has hundreds of them, and the nearest hundred to one of their
              -- own faces are all hers — which came back as nobody at all.
              AND c.person_id <> f.person_id
            ORDER BY c.centroid <=> f.embedding
            LIMIT 60
        ) near
        JOIN photo_people pe ON pe.id = near.person_id
        WHERE f.id = ANY(:faces)
        ORDER BY near.d
    """), {"faces": [face for _, face in faces]})).all()

    nearest: dict[int, list[dict]] = {}
    for face, person, name, distance in rows:
        theirs = nearest.setdefault(face, [])
        if any(one["id"] == person for one in theirs):
            continue
        theirs.append({"id": person, "name": name, "distance": float(distance)})

    for pid, face in faces:
        who[pid][0]["like"] = nearest.get(face, [])
