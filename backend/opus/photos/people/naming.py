"""What a person decides about who is in the photographs, written onto the faces.

A name lives on the faces as well as on a group: the group is a guess the machine
may revise, and a fresh group inherits whoever its faces already belong to."""

import datetime
from dataclasses import dataclass

from sqlalchemy import delete, func, select, text

from opus.models import Face, FaceCluster, Person
from opus.photos.people import cluster, lives, portraits, recognise

# How much nearer than the next person a face must lean before the lean is worth
# showing. Measured on a group welded out of two sisters: at 0.05 the twelve
# faces it offered to a third, unrelated child — average distance 0.335, average
# margin 0.026 — all disappear, and twenty-six of the two sisters' own survive.
LEANS = 0.05


class NotFound(Exception):
    pass


class Unnamed(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass
class BeforeBorn(Exception):
    person: str
    born_on: datetime.date
    first: datetime.date
    dated: int

    def said(self) -> dict:
        return {"reason": "before they were born", "person": self.person,
                "born_on": self.born_on.isoformat(),
                "earliest_photograph": self.first.isoformat(), "dated_faces": self.dated}


def split_name(full: str) -> tuple[str, str]:
    """The last word is the family name and everything before it the given name,
    which is how these names are typed. One word is a given name: somebody known
    only as Baka has no surname, she has a name."""
    parts = full.split()
    if len(parts) < 2:
        return full.strip(), ""
    return " ".join(parts[:-1]), parts[-1]


def shown(person: Person) -> dict:
    return {"id": person.id, "name": person.name,
            "given_name": person.given_name, "family_name": person.family_name,
            "born_on": person.born_on.isoformat() if person.born_on else None,
            "family": person.family}


async def _named(session, name: str, other_than: int | None = None) -> Person | None:
    query = select(Person).where(func.lower(Person.name) == name.lower())
    if other_than is not None:
        query = query.where(Person.id != other_than)
    return (await session.execute(query)).scalar_one_or_none()


async def add(session, name: str | None, given: str | None, family: str | None,
              born_on: datetime.date | None) -> Person:
    given, family = (given or "").strip(), (family or "").strip()
    name = (name or " ".join(x for x in (given, family) if x)).strip()
    if not name:
        raise Unnamed("a person needs a name")
    if not given and not family:
        given, family = split_name(name)
    if existing := await _named(session, name):
        raise Conflict(f"{existing.name} is already here")
    person = Person(name=name, given_name=given, family_name=family, born_on=born_on)
    session.add(person)
    await session.commit()
    return person


async def whom(session, person_id: int | None, name: str | None,
               born_on: datetime.date | None) -> Person:
    """The person named: by id, or by name — made if nobody has it."""
    if person_id is not None:
        person = await session.get(Person, person_id)
        if person is None:
            raise NotFound("no such person")
        return person
    if not (name and name.strip()):
        raise Unnamed("give either a person or a name")
    name = name.strip()
    person = await _named(session, name)
    if person is None:
        given, family = split_name(name)
        person = Person(name=name, given_name=given, family_name=family, born_on=born_on)
        session.add(person)
        await session.flush()
    return person


async def _span(session, where: str, bound: dict) -> tuple:
    return (await session.execute(text(f"""
        SELECT min(ph.taken_at)::date, max(ph.taken_at)::date, count(*)
        FROM photo_faces f JOIN photos ph ON ph.id = f.photo_id
        WHERE {where} AND ph.taken_at IS NOT NULL
    """), bound)).one()


def _refuse_before_birth(person: Person, first, dated: int, ignore_dates: bool) -> None:
    # The one thing a resemblance may not overrule. A child is not in a
    # photograph taken before she was born, however alike two sisters look at
    # the same age — and looking alike at the same age is exactly the case this
    # is here for.
    if person.born_on and first and not ignore_dates and first < person.born_on:
        raise BeforeBorn(person.name, person.born_on, first, dated)


def _years(first, last) -> list:
    return [first.year if first else None, last.year if last else None]


async def first_photographed(session, group: int):
    return (await _span(session, "f.cluster_id = :cid", {"cid": group}))[0]


async def give_group(session, group: int, person_id: int | None) -> None:
    await session.execute(
        FaceCluster.__table__.update().where(FaceCluster.id == group)
        .values(person_id=person_id))
    await session.execute(
        Face.__table__.update().where(Face.cluster_id == group)
        .values(person_id=person_id))


async def _owners_of_faces(session, faces: list[int]) -> set[int]:
    return set((await session.execute(
        select(Face.person_id).where(Face.id.in_(faces),
                                     Face.person_id.isnot(None)))).scalars())


async def _owners_of_groups(session, groups: list[int]) -> set[int]:
    return set((await session.execute(
        select(FaceCluster.person_id).where(FaceCluster.id.in_(groups),
                                            FaceCluster.person_id.isnot(None)))).scalars())


async def _groups_of(session, faces: list[int]) -> list[int]:
    return list((await session.execute(
        select(Face.cluster_id).distinct()
        .where(Face.id.in_(faces), Face.cluster_id.isnot(None)))).scalars())


async def _regrouped(session, groups: list[int]) -> None:
    """The groups faces just left: gone if empty, their middle moved if not."""
    await session.flush()
    await cluster.drop_empty(session)
    await cluster.refresh_centroids(session, groups)


def _renamed(person: Person, name: str | None, given: str | None,
             family: str | None) -> tuple[str, str, str] | None:
    if given is not None or family is not None:
        given = (given if given is not None else person.given_name).strip()
        family = (family if family is not None else person.family_name).strip()
        return " ".join(x for x in (given, family) if x), given, family
    if name is not None:
        return name.strip(), *split_name(name.strip())
    return None


async def _hand_back_impossible(session, person: Person) -> list[dict]:
    impossible = (await session.execute(text("""
        SELECT c.id, count(*), min(p.taken_at)::date
        FROM photo_face_clusters c
        JOIN photo_faces f ON f.cluster_id = c.id
        JOIN photos p ON p.id = f.photo_id
        WHERE c.person_id = :pid AND p.taken_at IS NOT NULL
        GROUP BY c.id HAVING min(p.taken_at)::date < :born
    """), {"pid": person.id, "born": person.born_on})).all()
    for cid, _, _ in impossible:
        await give_group(session, cid, None)
    return [{"cluster": cid, "faces": n, "from": first.isoformat()}
            for cid, n, first in impossible]


async def amend(session, person_id: int, name: str | None, given: str | None,
                family: str | None, born_on: datetime.date | None,
                kin: bool | None = None) -> dict:
    """A real birth date replaces the estimate, so every group already on the
    person is asked again whether it can be theirs, and the ones holding
    photographs from before they were born are handed back."""
    person = await session.get(Person, person_id)
    if person is None:
        raise NotFound("no such person")
    if (renamed := _renamed(person, name, given, family)) is not None:
        if not renamed[0]:
            raise Unnamed("a person needs a name")
        if clash := await _named(session, renamed[0], other_than=person_id):
            raise Conflict(f"{clash.name} is already here")
        person.name, person.given_name, person.family_name = renamed
    if born_on is not None:
        if person.born_source == "contacts":
            raise Conflict(f"{person.name} is dated from the contacts book; change it there")
        person.born_on, person.born_source = born_on, "typed"
    if kin is not None:
        person.family = kin
    await session.flush()
    handed_back = await _hand_back_impossible(session, person) if person.born_on else []
    await session.commit()
    return {**shown(person), "born_source": person.born_source or None,
            "handed_back": handed_back}


async def attach(session, group: int, person_id: int | None, name: str | None,
                 born_on: datetime.date | None, ignore_dates: bool) -> dict:
    if await session.get(FaceCluster, group) is None:
        raise NotFound("no such group")
    person = await whom(session, person_id, name, born_on)
    first, last, dated = await _span(session, "f.cluster_id = :cid", {"cid": group})
    _refuse_before_birth(person, first, dated, ignore_dates)
    await give_group(session, group, person.id)
    await session.commit()
    return {"cluster": group, "person": {"id": person.id, "name": person.name},
            "years": _years(first, last)}


async def detach(session, group: int) -> dict:
    if await session.get(FaceCluster, group) is None:
        raise NotFound("no such group")
    await give_group(session, group, None)
    await session.commit()
    return {"cluster": group, "person": None}


async def discard(session, group: int) -> dict:
    if await session.get(FaceCluster, group) is None:
        raise NotFound("no such group")
    faces = (await session.execute(
        delete(Face).where(Face.cluster_id == group).returning(Face.id))).all()
    await session.execute(delete(FaceCluster).where(FaceCluster.id == group))
    await session.commit()
    return {"cluster": group, "faces": len(faces)}


async def detach_faces(session, config, faces: list[int]) -> dict:
    if not faces:
        return {"faces": 0}
    owners = await _owners_of_faces(session, faces)
    groups = await _groups_of(session, faces)
    loosened = (await session.execute(
        Face.__table__.update().where(Face.id.in_(faces))
        .values(person_id=None, cluster_id=None).returning(Face.id))).all()
    await _regrouped(session, groups)
    await session.commit()
    return {"faces": len(loosened), "reels": portraits.forget_morphs(config, owners)}


async def discard_faces(session, config, faces: list[int]) -> dict:
    if not faces:
        return {"faces": 0}
    owners = await _owners_of_faces(session, faces)
    groups = await _groups_of(session, faces)
    gone = (await session.execute(
        delete(Face).where(Face.id.in_(faces)).returning(Face.id))).all()
    await _regrouped(session, groups)
    await session.commit()
    return {"faces": len(gone), "reels": portraits.forget_morphs(config, owners)}


async def give_faces(session, config, faces: list[int], person_id: int | None,
                     name: str | None, born_on: datetime.date | None,
                     ignore_dates: bool) -> dict:
    if not faces:
        return {"faces": 0}
    person = await whom(session, person_id, name, born_on)
    first, last, dated = await _span(session, "f.id = ANY(:ids)", {"ids": faces})
    _refuse_before_birth(person, first, dated, ignore_dates)
    owners = await _owners_of_faces(session, faces)
    groups = await _groups_of(session, faces)
    moved = (await session.execute(
        Face.__table__.update().where(Face.id.in_(faces))
        .values(person_id=person.id, cluster_id=None).returning(Face.id))).all()
    await _regrouped(session, groups)
    await session.commit()
    return {"faces": len(moved), "person": {"id": person.id, "name": person.name},
            "years": _years(first, last),
            "reels": portraits.forget_morphs(config, owners | {person.id})}


async def detach_groups(session, config, groups: list[int]) -> dict:
    if not groups:
        return {"clusters": 0, "faces": 0}
    owners = await _owners_of_groups(session, groups)
    faces = (await session.execute(
        Face.__table__.update().where(Face.cluster_id.in_(groups))
        .values(person_id=None).returning(Face.id))).all()
    loosened = (await session.execute(
        FaceCluster.__table__.update().where(FaceCluster.id.in_(groups))
        .values(person_id=None).returning(FaceCluster.id))).all()
    await session.commit()
    return {"clusters": len(loosened), "faces": len(faces),
            "reels": portraits.forget_morphs(config, owners)}


async def discard_groups(session, config, groups: list[int]) -> dict:
    if not groups:
        return {"clusters": 0, "faces": 0}
    owners = await _owners_of_groups(session, groups)
    faces = (await session.execute(
        delete(Face).where(Face.cluster_id.in_(groups)).returning(Face.id))).all()
    gone = (await session.execute(
        delete(FaceCluster).where(FaceCluster.id.in_(groups))
        .returning(FaceCluster.id))).all()
    await session.commit()
    return {"clusters": len(gone), "faces": len(faces),
            "reels": portraits.forget_morphs(config, owners)}


async def adopt_runs(session) -> dict:
    """Every unnamed group in a named run goes to that person; a group holding
    photographs from before the person was born is reported rather than given,
    because that is two people who look alike having been chained."""
    found = await lives.runs(session)
    born_on = dict((await session.execute(select(Person.id, Person.born_on))).all())
    given, refused, touched = 0, [], set()
    for life in found:
        who = life.get("person")
        if not who:
            continue
        for member in life["groups"]:
            if member.get("person"):
                continue
            first = await first_photographed(session, member["id"])
            born = born_on.get(who["id"])
            if born and first and first < born:
                refused.append({"cluster": member["id"], "person": who["name"],
                                "earliest_photograph": first.isoformat()})
                continue
            await give_group(session, member["id"], who["id"])
            given += 1
            touched.add(who["name"])
    await session.commit()
    return {"groups": given, "people": sorted(touched), "refused": refused}


async def name_life(session, groups: list[int], person_id: int | None, name: str | None,
                    born_on: datetime.date | None, ignore_dates: bool) -> dict:
    person = await whom(session, person_id, name, born_on)
    given, refused = [], []
    for cid in groups:
        first = await first_photographed(session, cid)
        if person.born_on and first and not ignore_dates and first < person.born_on:
            refused.append({"cluster": cid, "earliest_photograph": first.isoformat()})
            continue
        await give_group(session, cid, person.id)
        given.append(cid)
    await session.commit()
    return {"person": {"id": person.id, "name": person.name},
            "given": given, "refused": refused,
            "born_on": person.born_on.isoformat() if person.born_on else None}


async def leanings(session, group: int) -> dict:
    """Which known person each face in a group leans towards: for a group welded
    out of two people, a proposal of how to take it apart. It selects; a person
    confirms."""
    rows = (await session.execute(text("""
        WITH me AS (
            SELECT f.id AS face, f.embedding,
                   EXTRACT(YEAR FROM p.taken_at)::int AS y,
                   p.taken_at::date AS on_day
              FROM photo_faces f
              JOIN photos p ON p.id = f.photo_id
             WHERE f.cluster_id = :cid AND p.taken_at IS NOT NULL
        ),
        lim AS (SELECT min(y) - :within AS lo, max(y) + :within AS hi FROM me),
        -- every year each named group was photographed in, narrowed first to the
        -- years that could possibly be near this group's own: the same question
        -- unnarrowed reads every named face in the library
        cy AS (
            SELECT DISTINCT kf.cluster_id AS cid,
                   EXTRACT(YEAR FROM kp.taken_at)::int AS y
              FROM photo_faces kf
              JOIN photo_face_clusters k
                ON k.id = kf.cluster_id AND k.person_id IS NOT NULL
              JOIN photos kp ON kp.id = kf.photo_id
             WHERE kp.taken_at IS NOT NULL
               AND EXTRACT(YEAR FROM kp.taken_at)
                   BETWEEN (SELECT lo FROM lim) AND (SELECT hi FROM lim)
        ),
        pairs AS (
            SELECT m.face, k.person_id, pp.name,
                   max(1 - (k.centroid <=> m.embedding)) AS sim
              FROM me m
              JOIN cy ON abs(cy.y - m.y) <= :within
              JOIN photo_face_clusters k ON k.id = cy.cid
              JOIN photo_people pp ON pp.id = k.person_id
             WHERE k.centroid IS NOT NULL
               AND (pp.born_on IS NULL OR m.on_day >= pp.born_on)
             GROUP BY m.face, k.person_id, pp.name
        ),
        ranked AS (
            SELECT *,
                   row_number() OVER (PARTITION BY face ORDER BY sim DESC) AS rn,
                   lead(sim) OVER (PARTITION BY face ORDER BY sim DESC) AS nxt
              FROM pairs
        )
        SELECT face, person_id, name, sim, sim - coalesce(nxt, 0) AS margin
          FROM ranked WHERE rn = 1
    """), {"cid": group, "within": recognise.WITHIN_YEARS})).all()

    kept = [r for r in rows if float(r.margin) >= LEANS]
    tally: dict[int, dict] = {}
    for r in kept:
        one = tally.setdefault(r.person_id, {"person_id": r.person_id,
                                             "name": r.name, "faces": 0})
        one["faces"] += 1
    return {
        "leanings": [{"face": r.face, "person_id": r.person_id, "name": r.name,
                      "sim": round(float(r.sim), 3),
                      "margin": round(float(r.margin), 3)} for r in kept],
        "people": sorted(tally.values(), key=lambda o: -o["faces"]),
    }
