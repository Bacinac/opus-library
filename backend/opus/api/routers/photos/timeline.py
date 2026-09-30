"""The shelf in the order it happened.

Two endpoints, because a grid of 44,701 photographs needs to know its own shape
before it knows its contents. `/buckets` answers "how many in each month" in one
small query, which is enough to size the scroll area and draw the scrubber; the
rows themselves arrive a page at a time as the reader gets to them.

The pagination is by (taken_at, id) rather than by offset. An offset walks the
whole table again for every page and, worse, shifts under a scan that is adding
photographs while somebody is scrolling — which is exactly what happens here.
"""

import datetime
from dataclasses import dataclass
from typing import Annotated
from zoneinfo import ZoneInfo

import opus_auth
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text, tuple_
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from opus.db import get_session
from opus.models import Face, FACE_GENERATION, Person, Photo, User
from opus.photos.library import derive, search
from opus.settings_store import current_runtime

router = APIRouter()

MAX_PAGE = 500

# How wide the day's window may grow, in days, and in what steps. To three days
# it grows one at a time: that is still this day, and a screen can say "give or
# take a day" about it. Past that it has stopped being an anniversary and is
# simply the photographs nearest this date, so it strides instead of crawling —
# an empty February is not worth thirty queries.
WINDOWS = (0, 1, 2, 3, 5, 7, 10, 14, 21, 30, 45)


def _family():
    """The family is whoever the roster's accounts point at, and the people
    marked as family besides — not a surname, which is shared by cousins the
    house has not seen in years. A guest is a visitor."""
    return (select(User.person_id)
            .where(User.person_id.isnot(None), User.role != opus_auth.GUEST,
                   User.disabled.is_(False))
            .union(select(Person.id).where(Person.family.is_(True))))


def _in_it(people):
    # EXISTS rather than a join: a photograph with three of their faces in it
    # is still one photograph
    return select(Face.id).where(Face.photo_id == Photo.id, people).exists()


@dataclass(frozen=True)
class Whose:
    """Whose photographs a screen is narrowed to, said once for every screen
    that narrows — the shelf, its rail of months, this day and the years — so
    that a person or the family means the same thing on each of them.

    Any of the people named in the picture; somebody of the family in it; or the
    household's, the family's or nobody's. The household is what a wall shows
    unasked: a shelf opened by hand may hold the neighbour's wedding, a
    screensaver in the living room may not, and a landscape is fine on both."""

    person: Annotated[list[int] | None, Query()] = None
    family: bool = False
    household: bool = False

    def __bool__(self) -> bool:
        return bool(self.person) or self.family or self.household

    def clauses(self) -> list:
        said = []
        if self.person:
            said.append(_in_it(Face.person_id.in_(self.person)))
        if self.family:
            said.append(_in_it(Face.person_id.in_(_family())))
        if self.household:
            said.append(~select(Face.id).where(Face.photo_id == Photo.id).exists()
                        | _in_it(Face.person_id.in_(_family())))
        return said


async def _asked(session, q: str) -> tuple[search.Reading, str]:
    config = await current_runtime()
    vocabulary = await search.vocabulary(session, derive.store_root(config))
    return search.read(q, vocabulary), config.get("photos_timezone") or "Europe/Zagreb"


def _term(term: search.Term, names: dict[int, str]) -> dict:
    shown = {"kind": term.kind, "said": term.said}
    if term.kind == "person":
        shown["people"] = sorted(({"id": i, "name": names[i]} for i in term.value),
                                 key=lambda p: p["name"])
    elif term.kind == "place":
        shown["places"] = [{"place": p, "country": c} for p, c in sorted(term.value)]
    elif term.kind == "country":
        shown["countries"] = sorted(term.value)
    return shown


def _shape(photo: Photo) -> dict:
    """What a tile needs and nothing else. The checksum is the address of both
    derivatives, so no path ever reaches the browser — a client cannot ask for a
    file, only for a photograph."""
    return {
        "id": photo.checksum.hex(),
        "kind": photo.kind,
        "taken_at": photo.taken_at.isoformat() if photo.taken_at else None,
        # why we believe that date; a timeline that cannot say invites the
        # reader to trust a filename as much as a camera
        "dated": photo.taken_source,
        "w": photo.pixel_w,
        "h": photo.pixel_h,
        # part of the derivatives' address: they are cached as immutable, and a
        # turned photograph under the old one is the unturned picture for a year
        "turn": photo.turn,
        # 21 bytes that draw the picture before its file has left the disk
        "hash": photo.thumbhash.hex() if photo.thumbhash else None,
        "ready": photo.derived_gen == derive.GENERATION,
        # not the same as "not ready yet": one is a photograph waiting for a
        # pass, the other is a photograph no pass will ever finish. A grid that
        # cannot tell them apart spins forever on the second.
        "undecodable": photo.derive_failed_gen == derive.GENERATION,
    }


@router.get("/places")
async def places_list(session=Depends(get_session)):
    """Everywhere the archive has been, and how much of it happened there.

    A place is worth showing only if the photographs agree on its name, so this
    counts what is stored rather than what could be worked out — a coordinate
    with no name is somewhere nobody has been told about yet."""
    rows = (await session.execute(text("""
        SELECT p.place, p.country, count(*) AS n,
               -- a point to pin it to. The middle of what was photographed
               -- there, not the middle of the town: a week on one beach should
               -- not put the pin in the town hall
               round(avg(p.latitude)::numeric, 5) AS lat,
               round(avg(p.longitude)::numeric, 5) AS lon,
               min(p.taken_at)::date AS first, max(p.taken_at)::date AS last,
               count(*) FILTER (WHERE p.place_source = 'typed') AS typed,
               (SELECT encode(c.checksum, 'hex') FROM photos c
                 WHERE c.place = p.place AND c.country = p.country
                   AND c.derived_gen = :gen
                 ORDER BY c.taken_at DESC NULLS LAST LIMIT 1) AS cover
        FROM photos p WHERE p.place <> ''
        GROUP BY p.place, p.country
        ORDER BY count(*) DESC
    """), {"gen": derive.GENERATION})).all()
    turned = dict((await session.execute(
        select(Photo.checksum, Photo.turn)
        .where(Photo.checksum.in_([bytes.fromhex(r.cover) for r in rows if r.cover]),
               Photo.turn != 0))).all())
    return [{
        "place": place, "country": country, "photographs": n,
        "at": ({"lat": float(lat), "lon": float(lon)} if lat is not None else None),
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "typed": typed, "cover": cover,
        "cover_turn": turned.get(bytes.fromhex(cover), 0) if cover else 0,
    } for place, country, n, lat, lon, first, last, typed, cover in rows]


@router.get("/search")
async def photo_search(q: str = Query(max_length=200), session=Depends(get_session)):
    """What each word of a search was taken to name, and the words that named
    nothing — so a shelf that comes back empty can say which word it was."""
    reading, _ = await _asked(session, q)
    ids = {i for term in reading.terms if term.kind == "person" for i in term.value}
    names = dict((await session.execute(
        select(Person.id, Person.name).where(Person.id.in_(ids)))).all()) if ids else {}
    return {"terms": [_term(t, names) for t in reading.terms],
            "unread": list(reading.unread)}


@router.get("/timeline/buckets")
async def timeline_buckets(place: str | None = None, whose: Whose = Depends(),
                           q: str = Query("", max_length=200),
                           session=Depends(get_session)):
    """How many photographs in each month, newest first.

    One query over an indexed column. This is what lets a scrubber know that
    July 2013 is a tall month and September 2020 is a thin one without asking
    for a single row.

    The month is taken in the household's zone, not UTC. A photograph made at
    one minute past midnight on the first of November is a November photograph;
    computed in UTC it is filed under October, and the reader is left wondering
    why a picture they remember taking is in the wrong month."""
    config = await current_runtime()
    zone = config.get("photos_timezone") or "Europe/Zagreb"
    month = func.to_char(func.timezone(zone, Photo.taken_at), "YYYY-MM")
    counted = (select(month.label("month"), func.count().label("n"))
               .where(Photo.taken_at.isnot(None)))
    nothing = select(func.count()).select_from(Photo).where(Photo.taken_at.is_(None))
    if place is not None:
        counted = counted.where(Photo.place == place)
        nothing = nothing.where(Photo.place == place)
    # the same narrowing the pages take, or the rail counts one library and the
    # shelf shows another — a person opened from the people view had a rail of
    # every year the household has, most of them empty for her
    for clause in whose.clauses():
        counted = counted.where(clause)
        nothing = nothing.where(clause)
    if q.strip():
        reading, zone = await _asked(session, q)
        counted = search.narrowed(counted, reading, zone)
        nothing = search.narrowed(nothing, reading, zone)
    rows = (await session.execute(
        counted.group_by(month).order_by(month.desc()))).all()
    undated = (await session.execute(nothing)).scalar_one()
    return {
        "months": [{"month": m, "count": n} for m, n in rows],
        "total": sum(n for _, n in rows),
        # kept separate rather than folded into a month it does not belong to
        "undated": undated,
    }


@router.get("/timeline")
async def timeline(
    before: datetime.datetime | None = None,
    before_id: int | None = None,
    month: str | None = None,
    place: str | None = None,
    whose: Whose = Depends(),
    q: str = Query("", max_length=200),
    limit: int = Query(200, ge=1, le=MAX_PAGE),
    session=Depends(get_session),
):
    """A page of the timeline, newest first.

    Pass back the `next` cursor from the previous page. `month` (YYYY-MM) jumps
    to the start of a month instead — what a grid needs when somebody drags the
    scrubber into 2013 rather than scrolling there. It is answered here and not
    computed by the caller because a month only means something in the
    household's zone, and a client that worked that out for itself would be a
    second place the zone lives.

    Photographs with no capture date are not in this stream at all — they have
    nowhere to sit on a timeline, and putting them at the epoch or at today
    would be inventing a place for them."""
    if (before is None) != (before_id is None):
        raise HTTPException(400, "before and before_id travel together")
    if month is not None and before is None:
        config = await current_runtime()
        zone = config.get("photos_timezone") or "Europe/Zagreb"
        try:
            year, mon = (int(x) for x in month.split("-"))
            if not 1 <= mon <= 12:
                raise ValueError(mon)
            start = datetime.datetime(year + mon // 12, mon % 12 + 1, 1)
        except (ValueError, TypeError):
            raise HTTPException(400, "month must be YYYY-MM")
        # the first instant of the month AFTER it, so the month itself is the
        # first thing the page returns. One timezone() and not two: it reads a
        # wall clock as being in `zone` and hands back the instant. Converting
        # again would hand back a wall clock, which Postgres then reads as UTC —
        # an hour's drift, and a photograph taken just after midnight on the
        # first appearing at the head of the month before.
        before = (await session.execute(
            select(func.timezone(zone, start)))).scalar_one()
        before_id = 0
    query = (select(Photo)
             .where(Photo.taken_at.isnot(None), *whose.clauses())
             .order_by(Photo.taken_at.desc(), Photo.id.desc())
             .limit(limit))
    if place is not None:
        query = query.where(Photo.place == place)
    if q.strip():
        reading, zone = await _asked(session, q)
        query = search.narrowed(query, reading, zone)
    if before is not None:
        # A row-wise comparison and not the OR that spells out the same thing:
        # both are correct, but Postgres reads this one as a single seek into
        # the (taken_at, id) index, where the OR makes it consider two ranges.
        # The id is the tie-break that stops a burst sharing one second from
        # repeating a row or skipping one at a page boundary.
        query = query.where(
            tuple_(Photo.taken_at, Photo.id) < tuple_(before, before_id))
    rows = (await session.execute(query)).scalars().all()
    return {
        "photos": [_shape(p) for p in rows],
        "next": ({"before": rows[-1].taken_at.isoformat(), "before_id": rows[-1].id}
                 if len(rows) == limit else None),
    }


@router.get("/onthisday")
async def on_this_day(
    month: int | None = Query(None, ge=1, le=12),
    day: int | None = Query(None, ge=1, le=31),
    span: int | None = Query(None, ge=0, le=3),
    least: int = Query(0, ge=0, le=MAX_PAGE),
    whose: Whose = Depends(),
    limit: int = Query(400, ge=1, le=MAX_PAGE),
    session=Depends(get_session),
):
    """This day, through the years it has been photographed.

    Grouped by year and newest year first, because that is how it is read: what
    was happening a year ago, then five, then fifteen.

    Measured over this archive, 336 days of the year carry something and 195 of
    them carry three years or more — so a day is usually enough on its own, and
    when it is not the window widens by a day at a time rather than answering
    with two photographs. It says how wide it ended up; a screen that cannot say
    "give or take a day" is a screen claiming a precision it does not have.

    `least` is for a screen that has to fill itself anyway — the wall in the
    living room, which shows one photograph every twenty seconds all evening and
    would otherwise show the same three all night. Given it, the window keeps
    widening past those three days until it holds that many photographs: no
    longer this day, but the nearest days to it, and it says so in `span`.

    The day is a wall-clock day in the household's zone, not a UTC one: a
    photograph taken at half past eleven at night belongs to the evening it was
    taken in, and comparing UTC dates moves a whole summer of late nights into
    the following day."""
    config = await current_runtime()
    zone = config.get("photos_timezone") or "Europe/Zagreb"
    if month is None or day is None:
        today = datetime.datetime.now(ZoneInfo(zone))
        month, day = today.month, today.day

    steps = ((span,) if span is not None
             else tuple(w for w in WINDOWS if w <= 3 or least))
    for wide in steps:
        rows = await _photos_of_days(session, zone, _days_around(month, day, wide),
                                     whose, limit)
        years = {p.taken_at.year for p in rows}
        if wide == steps[-1] or (len(years) >= 3 and len(rows) >= least):
            break
    named, in_it = await _who_the_years_were_about(session, rows)
    return {
        "month": month,
        "day": day,
        "span": wide,
        "years": _year_cards(rows, named, in_it),
        "total": len(rows),
    }


@router.get("/years")
async def through_the_years(
    whose: Whose = Depends(),
    each: int = Query(12, ge=1, le=100),
    session=Depends(get_session),
):
    """One person through the years, or the family: a handful drawn at random
    out of every year they are in, in the shape of a day through the years.

    For a screen that shows one photograph at a time for as long as it is left
    on. The shelf newest first would spend the whole evening on last summer; a
    handful from every year lets the screen take the years in turns, and asking
    again draws another handful. Never everybody: the whole library in turns
    is not a screen anybody asked for."""
    if not whose:
        raise HTTPException(400, "whose years: a person, the family or the household")
    zone = (await current_runtime()).get("photos_timezone") or "Europe/Zagreb"
    drawn = (select(Photo.id, func.row_number().over(
                 partition_by=func.extract("year", func.timezone(zone, Photo.taken_at)),
                 order_by=func.random()).label("n"))
             .where(Photo.taken_at.isnot(None), *whose.clauses())
             .subquery())
    rows = (await session.execute(
        select(Photo).join(drawn, drawn.c.id == Photo.id).where(drawn.c.n <= each)
        .order_by(Photo.taken_at.desc(), Photo.id.desc()))).scalars().all()
    named, in_it = await _who_the_years_were_about(session, rows)
    return {"years": _year_cards(rows, named, in_it), "total": len(rows)}


def _days_around(month: int, day: int, wide: int) -> list[tuple[int, int]]:
    """Which days count, as (month, day) pairs, worked out on a leap year so
    that the 29th of February is a day like any other rather than a hole the
    window falls into."""
    middle = datetime.date(2024, month, min(day, 29 if month == 2 else 31))
    return sorted({
        ((middle + datetime.timedelta(days=off)).month,
         (middle + datetime.timedelta(days=off)).day)
        for off in range(-wide, wide + 1)
    })


async def _photos_of_days(session, zone: str, days: list[tuple[int, int]],
                          whose: Whose, limit: int) -> list[Photo]:
    local = func.timezone(zone, Photo.taken_at)
    query = (select(Photo)
             .where(Photo.taken_at.isnot(None), *whose.clauses())
             .where(tuple_(func.extract("month", local),
                           func.extract("day", local)).in_(days))
             .order_by(Photo.taken_at.desc(), Photo.id.desc())
             .limit(limit))
    return (await session.execute(query)).scalars().all()


async def _who_the_years_were_about(
        session, rows: list[Photo]) -> tuple[dict[int, list[dict]], dict[int, set[int]]]:
    """A card saying "twenty-one years ago" says when; the names say what, and
    they are the reason somebody presses it. Counted by faces so the person the
    day was actually about comes first, and asked in one query for the whole
    day rather than one per year. The second answer is who is in each
    photograph, which is what makes one of them the face of its year and what
    a photograph shown alone is captioned with: a card about Eva, Kata and Filip
    showing the goat they walked past is a card about a goat."""
    named: dict[int, list[dict]] = {}
    in_it: dict[int, set[int]] = {}
    if not rows:
        return named, in_it
    at_year = {p.id: p.taken_at.year for p in rows}
    faces = (await session.execute(
        select(Face.photo_id, Person.id, Person.name, Person.given_name)
        .join(Person, Person.id == Face.person_id)
        .where(Face.photo_id.in_(list(at_year))))).all()
    tally: dict[int, dict[int, dict]] = {}
    for photo_id, person_id, whole, given in faces:
        year = tally.setdefault(at_year[photo_id], {})
        one = year.setdefault(person_id, {"id": person_id, "name": given or whole, "faces": 0})
        one["faces"] += 1
        in_it.setdefault(photo_id, set()).add(person_id)
    for year, people in tally.items():
        named[year] = sorted(people.values(), key=lambda one: -one["faces"])
    return named, in_it


def _year_cards(rows: list[Photo], named: dict[int, list[dict]],
                in_it: dict[int, set[int]]) -> list[dict]:
    grouped: dict[int, list[Photo]] = {}
    for photo in rows:
        grouped.setdefault(photo.taken_at.year, []).append(photo)
    return [{"year": year,
             "people": named.get(year, []),
             # the picture that stands for the year: the one with the most
             # of its people in it, and the first of them where nobody is
             "cover": max(
                 grouped[year],
                 key=lambda one: (len(in_it.get(one.id, ())), -one.id),
             ).checksum.hex(),
             "photographs": [
                 {**_shape(p), "people": [one["name"] for one in named.get(year, [])
                                          if one["id"] in in_it.get(p.id, ())]}
                 for p in grouped[year]]}
            for year in sorted(grouped, reverse=True)]


class Place(BaseModel):
    """A name for where a photograph was taken, and how far it should reach."""
    name: str
    # a trip is not entered one photograph at a time: the same name is meant for
    # everything taken that day, or across a run of days
    day: bool = False
    from_day: datetime.date | None = None
    to_day: datetime.date | None = None
    # a name never lands on a photograph that already carries one somebody typed
    over_typed: bool = False


@router.put("/{checksum}/place")
async def name_the_place(checksum: str, body: Place, session=Depends(get_session)):
    """Say where a photograph was taken.

    Nothing is inferred here and nothing is guessed from neighbours: a camera
    without a receiver recorded nothing, and the only honest source for those
    twenty thousand photographs is the person who was there. It is written down
    as typed, so a name worked out later from coordinates can fill the gaps
    without ever overwriting one somebody meant."""
    try:
        digest = bytes.fromhex(checksum)
    except ValueError:
        raise HTTPException(400, "not a checksum")
    photo = (await session.execute(
        select(Photo).where(Photo.checksum == digest))).scalar_one_or_none()
    if photo is None:
        raise HTTPException(404, "no such photograph")

    name = body.name.strip()[:120]
    where = [Photo.id == photo.id]
    if body.day or body.from_day or body.to_day:
        if photo.taken_at is None and not (body.from_day and body.to_day):
            raise HTTPException(422, "this photograph has no date to spread a place along")
        zone = ZoneInfo((await current_runtime()).get("photos_timezone")
                        or "Europe/Zagreb")
        if body.from_day and body.to_day:
            start, end = body.from_day, body.to_day
        else:
            here = photo.taken_at.astimezone(zone).date()
            start = end = here
        first = (await session.execute(select(func.timezone(
            str(zone), datetime.datetime.combine(start, datetime.time.min))))).scalar_one()
        last = (await session.execute(select(func.timezone(
            str(zone), datetime.datetime.combine(end, datetime.time.max))))).scalar_one()
        where = [Photo.taken_at >= first, Photo.taken_at <= last]
    # The guard is there so a name spread across a day does not stomp on places
    # typed one at a time. It has no business stopping somebody clearing what
    # they typed, or naming a single photograph on purpose.
    if not body.over_typed and name and (body.day or body.from_day or body.to_day):
        where.append(Photo.place_source != "typed")

    changed = (await session.execute(
        Photo.__table__.update().where(*where)
        .values(place=name, place_source="typed" if name else "")
        .returning(Photo.id))).all()
    await session.commit()
    return {"place": name or None, "photographs": len(changed)}


@router.get("/{checksum}")
async def photo_detail(checksum: str, session=Depends(get_session)):
    """One photograph, with every path it is found at.

    The paths are shown because a person who is about to delete something is
    entitled to know where it actually is — and because 313 photographs in this
    library are at more than one."""
    try:
        digest = bytes.fromhex(checksum)
    except ValueError:
        raise HTTPException(400, "not a checksum")
    photo = (await session.execute(
        select(Photo).options(selectinload(Photo.files))
        .where(Photo.checksum == digest))).scalar_one_or_none()
    if photo is None:
        raise HTTPException(404, "no such photograph")
    return {
        **_shape(photo),
        "bytes": photo.byte_size,
        "device": " ".join(x for x in (photo.device_make, photo.device_model) if x),
        "offset": photo.taken_offset,
        "files": [{"path": f.path, "state": f.state,
                   "missing_since": f.missing_since.isoformat() if f.missing_since else None}
                  for f in photo.files],
        "live_pair": photo.live_pair_id is not None,
        "where": ({"lat": photo.latitude, "lon": photo.longitude,
                   "alt": photo.altitude} if photo.latitude is not None else None),
        "place": photo.place or None,
        "country": photo.country or None,
        "place_source": photo.place_source or None,
        "camera": {k: v for k, v in (
            ("lens", photo.lens), ("focal_mm", photo.focal_mm),
            ("aperture", photo.aperture), ("iso", photo.iso),
            ("shutter", photo.shutter)) if v},
        # who is in the picture, and where. Not a tag alongside the faces but the
        # faces themselves: the row that says somebody is here also says where
        # they are, and a second place to record the same fact would drift from
        # this one the moment a face moved
        "faces": [{
            "id": fid, "x": x, "y": y, "w": w, "h": h,
            "person": {"id": pid, "name": pname} if pid else None,
        } for fid, x, y, w, h, pid, pname in (await session.execute(text("""
            SELECT f.id, f.x, f.y, f.w, f.h, f.person_id, pp.name
            FROM photo_faces f
            LEFT JOIN photo_people pp ON pp.id = f.person_id
            WHERE f.photo_id = :pid AND f.generation = :gen
            ORDER BY f.person_id NULLS LAST, f.score DESC
        """), {"pid": photo.id, "gen": FACE_GENERATION})).all()],
    }
