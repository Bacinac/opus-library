"""Looking at what the machine grouped, before anybody is named.

A cluster is shown by one face and a count, because that is what a person
scanning a wall of them actually reads. The face is cropped from the preview on
demand rather than stored: a crop is a rectangle of a picture we already hold,
and 67,492 of them on disk would be a third derivative to keep in step with the
other two.

The date span is not stored either. A cluster has one because its faces have
dates, which is the whole reason "Jana 2008–2013" needs no field.
"""

import datetime

import opus_auth
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from opus_core.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, text

from opus import auth
from opus.api.routers.photos.shared import unmoved
from opus.db import get_session
from opus.models import FACE_GENERATION, Person
from opus.photos.people import contacts, lives, naming, portraits
from opus.settings_store import current_runtime

router = APIRouter()


class NewPerson(BaseModel):
    name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    born_on: datetime.date | None = None


class NameLife(BaseModel):
    clusters: list[int]
    person_id: int | None = None
    name: str | None = None
    born_on: datetime.date | None = None
    ignore_dates: bool = False


class Amend(BaseModel):
    name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    born_on: datetime.date | None = None
    family: bool | None = None


class Attach(BaseModel):
    person_id: int | None = None
    name: str | None = None
    born_on: datetime.date | None = None
    # a date says somebody was not alive when the picture was taken, and that is
    # not an opinion to be overruled by a resemblance. Set this only to say the
    # date on the photographs is what is wrong.
    ignore_dates: bool = False


@router.get("/clusters")
async def clusters(
    limit: int = Query(60, ge=1, le=300),
    after: int = Query(0, ge=0),
    named: bool | None = None,
    min_size: int = Query(2, ge=1),
    session=Depends(get_session),
):
    """The groups, largest first. Singletons are excluded by default: 27,432 of
    them is not a wall anybody reads, and a face alone is not yet a likeness."""
    where = ["f.generation = :gen", "f.cluster_id IS NOT NULL"]
    params: dict = {"gen": FACE_GENERATION, "limit": limit, "min": min_size,
                    "after": after}
    having = "count(*) >= :min"
    if named is True:
        where.append("c.person_id IS NOT NULL")
    elif named is False:
        where.append("c.person_id IS NULL")

    rows = (await session.execute(text(f"""
        SELECT c.id, c.person_id, p.name,
               count(*) AS faces,
               min(EXTRACT(YEAR FROM ph.taken_at))::int AS first_year,
               max(EXTRACT(YEAR FROM ph.taken_at))::int AS last_year,
               (SELECT f2.id FROM photo_faces f2
                 WHERE f2.cluster_id = c.id ORDER BY f2.score DESC LIMIT 1) AS cover
        FROM photo_face_clusters c
        JOIN photo_faces f ON f.cluster_id = c.id
        JOIN photos ph ON ph.id = f.photo_id
        LEFT JOIN photo_people p ON p.id = c.person_id
        WHERE {' AND '.join(where)}
        GROUP BY c.id, c.person_id, p.name
        HAVING {having}
        ORDER BY count(*) DESC, c.id
        OFFSET :after LIMIT :limit
    """), params)).all()

    return {
        "clusters": [{
            "id": cid,
            "person": {"id": pid, "name": name} if pid else None,
            "faces": n,
            "years": [y1, y2],
            "cover": cover,
        } for cid, pid, name, n, y1, y2, cover in rows],
        "next": after + len(rows) if len(rows) == limit else None,
    }


@router.get("/clusters/{cluster_id}")
async def cluster_faces(
    cluster_id: int,
    limit: int = Query(120, ge=1, le=500),
    after: str | None = Query(None, pattern=r"^-?[0-9.e+-]+:[0-9]+$"),
    session=Depends(get_session),
):
    """The faces in one group, best fit first. `after` is the `next` of the page
    before, the fit and id of its last face."""
    fit_after, id_after = (float(after.split(":")[0]), int(after.split(":")[1])) if after else (None, None)
    # Ordered by how far each face sits from the middle of its own group, not by
    # how sure the detector was that it was a face at all. Those are different
    # questions, and only the first one shows a stranger: whoever does not
    # belong drifts to the end, where a person scrolling finds them without
    # having to look at every picture.
    rows = (await session.execute(text("""
        WITH centre AS (
            SELECT AVG(embedding::vector) AS v
            FROM photo_faces WHERE cluster_id = :cid
        ), fitted AS (
            SELECT f.id, f.score, encode(ph.checksum, 'hex') AS checksum,
                   ph.taken_at, f.person_id, pp.name,
                   (1 - (f.embedding::vector <=> (SELECT v FROM centre))) AS fit
            FROM photo_faces f
            JOIN photos ph ON ph.id = f.photo_id
            LEFT JOIN photo_people pp ON pp.id = f.person_id
            WHERE f.cluster_id = :cid
        )
        SELECT * FROM fitted
        WHERE CAST(:fit AS double precision) IS NULL
           OR fit < :fit OR (fit = :fit AND id > :id)
        ORDER BY fit DESC, id
        LIMIT :limit
    """), {"cid": cluster_id, "fit": fit_after, "id": id_after, "limit": limit})).all()
    if not rows and after is None:
        raise HTTPException(404, "no such cluster")
    # The group itself, not only its faces: the run view opens a group without
    # holding a row about it.
    about = (await session.execute(text("""
        SELECT count(*), min(EXTRACT(YEAR FROM p.taken_at))::int,
               max(EXTRACT(YEAR FROM p.taken_at))::int, c.person_id, pp.name
        FROM photo_face_clusters c
        JOIN photo_faces f ON f.cluster_id = c.id
        LEFT JOIN photos p ON p.id = f.photo_id
        LEFT JOIN photo_people pp ON pp.id = c.person_id
        WHERE c.id = :cid
        GROUP BY c.id, c.person_id, pp.name
    """), {"cid": cluster_id})).first()
    return {
        "id": cluster_id,
        "faces_total": about[0] if about else 0,
        "years": [about[1], about[2]] if about else [None, None],
        "person": {"id": about[3], "name": about[4]} if about and about[3] else None,
        "faces": [{
            "id": fid, "score": round(float(score), 3), "photo": checksum,
            "taken_at": taken.isoformat() if taken else None,
            "person": {"id": pid, "name": name} if pid else None,
            # how well this face fits the group it is in; the low ones are the
            # ones worth a second look
            "fit": round(float(fit), 3),
        } for fid, score, checksum, taken, pid, name, fit in rows],
        "next": f"{rows[-1].fit!r}:{rows[-1].id}" if len(rows) == limit else None,
    }


async def _served(work):
    try:
        return await work
    except portraits.NotFound as why:
        raise HTTPException(404, str(why))
    except portraits.Uncuttable as why:
        raise HTTPException(422, str(why))
    except (portraits.CardBusy, portraits.FacesUnavailable) as why:
        raise HTTPException(503, str(why))
    except portraits.FacesRefused as refused:
        raise HTTPException(refused.status, refused.said)


# a derivative named by what it is made of, so it is cached as hard as its inputs
CACHED = {"Cache-Control": "public, max-age=31536000, immutable"}


@router.get("/faces/{face_id}/crop")
async def face_crop(face_id: int, size: str = Query("face", pattern="^(face|big)$"),
                    session=Depends(get_session)):
    """One face, cut out of the preview we already keep. Two sizes, because a
    grid of a hundred and twenty-three wants them small and a face that IS the
    screen would be a smear at 256 pixels."""
    config = await current_runtime()
    kept = await _served(portraits.crop(session, config, face_id, big=size == "big"))
    return FileResponse(kept, media_type="image/jpeg", headers=CACHED)


@router.get("/people/{person_id}/transformation")
async def transformation(person_id: int, session=Depends(get_session)):
    return await _served(portraits.transformation(session, await current_runtime(), person_id))


async def _admin_asking(request: Request, session=Depends(get_session)) -> bool:
    cookie = request.cookies.get(opus_auth.SESSION_COOKIE)
    return auth.standing(await auth.roster(session), cookie) == opus_auth.ADMIN


@router.get("/people/{person_id}/morph")
async def morph(person_id: int, size: int = 448, steps: int = portraits.MORPH_STEPS,
                admin: bool = Depends(_admin_asking), session=Depends(get_session)):
    """The run of portraits as one face becoming the next. How it moves is the
    admin's to vary: every other caller gets the one the pages show, so the card
    makes at most one morph per size per person for them."""
    if size not in portraits.MORPH_SIZES:
        raise HTTPException(
            400, f"a morph is drawn at {' or '.join(map(str, portraits.MORPH_SIZES))}")
    if not 1 <= steps <= portraits.MORPH_STEPS:
        raise HTTPException(400, f"a step count between 1 and {portraits.MORPH_STEPS}")
    if steps != portraits.MORPH_STEPS and not admin:
        raise HTTPException(403, "only an admin chooses how a morph moves")
    kept = await _served(portraits.morph(session, await current_runtime(),
                                         person_id, size, steps))
    return FileResponse(kept, media_type="image/webp", headers=CACHED)


@router.get("/faces/{face_id}/portrait")
async def face_portrait(face_id: int, session=Depends(get_session)):
    config = await current_runtime()
    kept = await _served(portraits.portrait(session, config, face_id))
    return FileResponse(kept, media_type="image/jpeg", headers=CACHED)


@router.get("/people")
async def people(session=Depends(get_session)):
    """Everybody named, with how much of the library they are in."""
    # The groups are counted in their own subquery, not joined in beside the
    # faces. Joined, the two multiply each other and a person with fifty groups
    # is reported as having fifty times her faces — 326,592 of them in a library
    # that holds 67,492 altogether.
    rows = (await session.execute(text("""
        SELECT p.id, p.name, p.given_name, p.family_name, p.born_on, p.born_source,
               p.family,
               (SELECT min(u.name) FROM users u
                 WHERE u.person_id = p.id AND u.role <> :guest
                   AND NOT u.disabled) AS account,
               (SELECT count(*) FROM photo_face_clusters c
                 WHERE c.person_id = p.id) AS clusters,
               -- the face's person and not its id: both say "there was a
               -- matching row", but only one of them is in the index this
               -- aggregate would otherwise have to leave to fetch
               count(f.person_id) AS faces,
               min(EXTRACT(YEAR FROM ph.taken_at))::int,
               max(EXTRACT(YEAR FROM ph.taken_at))::int,
               COALESCE(
                   (SELECT c.id FROM photo_faces c
                     WHERE c.id = p.cover_face_id AND c.person_id = p.id),
                   (SELECT f2.id FROM photo_faces f2
                     WHERE f2.person_id = p.id ORDER BY f2.score DESC LIMIT 1))
        FROM photo_people p
        LEFT JOIN photo_faces f ON f.person_id = p.id
        LEFT JOIN photos ph ON ph.id = f.photo_id
        GROUP BY p.id, p.name, p.given_name, p.family_name, p.born_on, p.born_source,
                 p.family, p.cover_face_id
        ORDER BY count(f.person_id) DESC, p.family_name, p.given_name
    """), {"guest": opus_auth.GUEST})).all()
    return [{
        "id": pid, "name": name, "given_name": given, "family_name": family,
        "born_on": born.isoformat() if born else None,
        "born_source": source or None,
        # family by the roster cannot be unmarked here: it goes with the account
        "family": kin or account is not None, "roster": account is not None,
        # whose face this is on the roster, which is how a player finds the
        # photographs of whoever's profile it has picked
        "account": account,
        "clusters": clusters, "faces": faces,
        "years": [y1, y2], "cover": cover,
    } for pid, name, given, family, born, source, kin, account, clusters, faces,
      y1, y2, cover in rows]


@router.get("/people/{person_id}/faces")
async def person_faces(
    person_id: int,
    limit: int = Query(120, ge=1, le=500),
    since: datetime.date | None = None,
    min_px: int = Query(0, ge=0),
    per_day: int = Query(0, ge=0),
    session=Depends(get_session),
):
    """The faces of one person, newest first, with where each one sits.

    Asked by a peer that enrols references from this library (BABA names the
    people its cameras see from the same photographs), so what comes back is
    what such a caller needs and nothing it does not: the photograph's
    checksum, so it can fetch the preview; the box as fractions of the frame,
    so it can cut the face out of that preview at any size; and how many
    pixels the face has in the original, so it can refuse one too small to
    say who it is. The vectors stay here — another model's space is not this
    one's, and a peer re-embeds pixels with its own.

    `since` and `min_px` filter the same way the run of portraits does: a
    face out of a recording is not a photograph, and a face under the width
    given carries nothing worth comparing. `per_day` caps how many faces one
    day of photographs may contribute, the day taken in the household's zone:
    a hundred frames of one afternoon are one look, and a caller after
    variety wants the days, not the frames."""
    person = (await session.execute(
        select(Person).where(Person.id == person_id))).scalar_one_or_none()
    if person is None:
        raise HTTPException(404, "no such person")
    zone = (await current_runtime()).get("photos_timezone") or "Europe/Zagreb"
    rows = (await session.execute(text("""
        SELECT id, checksum, taken_at, x, y, w, h, score, sharpness, px FROM (
            SELECT f.id, encode(p.checksum, 'hex') AS checksum, p.taken_at,
                   f.x, f.y, f.w, f.h, f.score, f.sharpness,
                   round(f.w * COALESCE(p.pixel_w, 2048))::int AS px,
                   row_number() OVER (
                       PARTITION BY (p.taken_at AT TIME ZONE :zone)::date
                       ORDER BY f.w * f.h DESC, f.id) AS nth
            FROM photo_faces f
            JOIN photos p ON p.id = f.photo_id
            WHERE f.person_id = :pid AND f.generation = :gen
              AND p.kind = 'image'
              AND (CAST(:since AS date) IS NULL OR p.taken_at >= :since)
              AND f.w * COALESCE(p.pixel_w, 2048) >= :min_px
        ) t WHERE :per_day = 0 OR nth <= :per_day
        ORDER BY taken_at DESC NULLS LAST, w * h DESC, id
        LIMIT :limit
    """), {"pid": person_id, "gen": FACE_GENERATION, "since": since,
           "min_px": min_px, "per_day": per_day, "zone": zone,
           "limit": limit})).all()
    return {
        "person": {"id": person.id, "name": person.name},
        "faces": [{
            "id": fid, "photo": checksum,
            "taken_at": taken.isoformat() if taken else None,
            "x": x, "y": y, "w": w, "h": h, "px": px,
            "score": round(float(score), 3),
            "sharpness": round(float(sharp), 1) if sharp is not None else None,
        } for fid, checksum, taken, x, y, w, h, score, sharp, px in rows],
    }


@router.post("/people", status_code=201)
async def add_person(body: NewPerson, session=Depends(get_session)):
    person = await _decided(naming.add(session, body.name, body.given_name,
                                       body.family_name, body.born_on))
    return naming.shown(person)


class Discard(BaseModel):
    clusters: list[int]


class DiscardFaces(BaseModel):
    faces: list[int]


class GiveFaces(Attach):
    faces: list[int]


async def _decided(work):
    try:
        return await work
    except naming.NotFound as why:
        raise HTTPException(404, str(why))
    except naming.Unnamed as why:
        raise HTTPException(422, str(why))
    except naming.Conflict as why:
        raise HTTPException(409, str(why))
    except naming.BeforeBorn as refused:
        raise HTTPException(409, refused.said())


@router.get("/clusters/{cluster_id}/leanings")
async def leanings(cluster_id: int, session=Depends(get_session)):
    return await naming.leanings(session, cluster_id)


@router.post("/faces/detach", status_code=200, dependencies=[Depends(unmoved)])
async def detach_faces(body: DiscardFaces, session=Depends(get_session)):
    """Take single faces off whoever they were given to. The faces stay, and
    leave their group as well: a face that kept its seat would be handed the
    name straight back the next time the library is grouped."""
    return await naming.detach_faces(session, await current_runtime(), body.faces)


@router.post("/faces/discard", status_code=200, dependencies=[Depends(unmoved)])
async def discard_faces(body: DiscardFaces, session=Depends(get_session)):
    """Throw single faces away: a face is a measurement of a picture, and the
    photograph is untouched."""
    return await naming.discard_faces(session, await current_runtime(), body.faces)


@router.post("/faces/person", status_code=200, dependencies=[Depends(unmoved)])
async def give_faces(body: GiveFaces, session=Depends(get_session)):
    """Give single faces to somebody, out of the group they are sitting in —
    what takes apart a group welded out of two sisters."""
    return await _decided(naming.give_faces(
        session, await current_runtime(), body.faces, body.person_id, body.name,
        body.born_on, body.ignore_dates))


@router.post("/clusters/detach", status_code=200, dependencies=[Depends(unmoved)])
async def detach_many(body: Discard, session=Depends(get_session)):
    """Take these groups off whoever they were given to. The faces stay, and
    lose the name too."""
    return await naming.detach_groups(session, await current_runtime(), body.clusters)


@router.post("/clusters/discard", status_code=200, dependencies=[Depends(unmoved)])
async def discard_many(body: Discard, session=Depends(get_session)):
    """Throw several groups away at once, faces and all."""
    return await naming.discard_groups(session, await current_runtime(), body.clusters)


@router.delete("/clusters/{cluster_id}", status_code=200, dependencies=[Depends(unmoved)])
async def discard(cluster_id: int, session=Depends(get_session)):
    """Throw a group away, and its faces with it: the next pass would build the
    same group again out of the same faces. Finding faces again restores them."""
    return await _decided(naming.discard(session, cluster_id))


@router.get("/contacts")
async def contacts_state(session=Depends(get_session)):
    """Whether the house can be asked, and how much of the library it has dated."""
    return await contacts.state(session)


@router.post("/contacts/sync")
async def contacts_sync(write: bool = False, everyone: bool = False,
                        session=Depends(get_session)):
    """Ask the house about the people this library knows."""
    return await contacts.sync(session, write=write, everyone=everyone)


@router.put("/people/lives", dependencies=[Depends(unmoved)])
async def name_a_life(body: NameLife, session=Depends(get_session)):
    """Give a whole run of years to one person in a single gesture; each group
    is still checked against the birth date, and refusals are reported."""
    return await _decided(naming.name_life(session, body.clusters, body.person_id,
                                           body.name, body.born_on, body.ignore_dates))


@router.put("/people/{person_id}", dependencies=[Depends(unmoved)])
async def amend(person_id: int, body: Amend, session=Depends(get_session)):
    """Correct what is known about a person — above all, when they were born."""
    return await _decided(naming.amend(session, person_id, body.name, body.given_name,
                                       body.family_name, body.born_on, body.family))


@router.put("/clusters/{cluster_id}/person", dependencies=[Depends(unmoved)])
async def attach(cluster_id: int, body: Attach, session=Depends(get_session)):
    """Give a group to somebody."""
    return await _decided(naming.attach(session, cluster_id, body.person_id, body.name,
                                        body.born_on, body.ignore_dates))


@router.delete("/clusters/{cluster_id}/person", dependencies=[Depends(unmoved)])
async def detach(cluster_id: int, session=Depends(get_session)):
    """Take the name back off a group, and off its faces with it."""
    return await _decided(naming.detach(session, cluster_id))


@router.get("/people/suggested")
async def suggested(
    limit: int = Query(40, ge=1, le=200),
    session=Depends(get_session),
):
    """The runs of groups that look like one person across the years."""
    found = await lives.runs(session)
    return {"lives": found[:limit], "total": len(found)}


@router.post("/people/lives/adopt", dependencies=[Depends(unmoved)])
async def adopt_the_runs(session=Depends(get_session)):
    """Write the runs down: every group in a named run goes to that person."""
    return await naming.adopt_runs(session)
