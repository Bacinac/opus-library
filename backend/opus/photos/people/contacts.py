"""Birth dates, asked of the house.

A birth date is the one fact about a person this library cannot work out and
cannot do without: two children who look alike at the same age are told apart by
nothing else, and the machine's estimate stands in until somebody has written the
real one down. Somebody wrote it in the household's address book.

That book is read by DIDA and by nothing else. Two readers would mean two consent
screens, two tokens to expire and two schedules that drift apart — so this asks
rather than reads, and a date that came back is not editable here: the book is
where it is kept, and a field editable in two places is two facts waiting to
disagree.

A date the photographs contradict is refused and reported. The book is where a
birth date is kept, but a date later than a photograph of the person is a
mistyped year rather than a disagreement — and this library exists to notice
exactly that kind of thing.

An ambiguous name comes back as nothing. DIDA refuses to choose between two
people of the same name, which is right: handing back one of them would put a
birth year on the wrong face, and keeping two faces apart is the entire reason a
birth year is wanted.
"""

import datetime
import logging

from opus_core.dida import DidaError
from sqlalchemy import func, select, text

from opus.models import Person
from opus.photos.people import house
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)


async def state(session) -> dict:
    config = await current_runtime()
    reachable, why = False, ""
    if config.get("dida_url"):
        try:
            await house.birthdays(config, within=0)
            reachable = True
        except DidaError as exc:
            why = str(exc)[:200]
    return {
        "configured": bool(config.get("dida_url")
                           and (config.get("dida_password") or config.get("dida_panel_key"))),
        "reachable": reachable, "why": why,
        "dated_from_the_book": (await session.execute(
            select(func.count()).select_from(Person)
            .where(Person.born_source == "contacts"))).scalar_one(),
        "people": (await session.execute(
            select(func.count()).select_from(Person))).scalar_one(),
    }


async def sync(session, write: bool = False, everyone: bool = False) -> dict:
    """Ask the house about the people this library knows.

    By default only about those it has not already matched to a contact — the
    book is asked once per person and a birth date does not change. `everyone`
    asks again, which is what to do after somebody has corrected the book."""
    config = await current_runtime()
    people = (await session.execute(select(Person).order_by(Person.name))).scalars().all()
    if not everyone:
        people = [p for p in people if not p.contact_id]

    # The earliest photograph of each person, which is the one thing here that can
    # contradict the book. A birth date later than a photograph of the person is
    # not a difference of opinion: somebody mistyped a year, and writing it would
    # silently break the single thing a birth date is kept for.
    earliest = dict((await session.execute(text("""
        SELECT f.person_id, min(p.taken_at)::date
        FROM photo_faces f JOIN photos p ON p.id = f.photo_id
        WHERE f.person_id IS NOT NULL AND p.taken_at IS NOT NULL
        GROUP BY f.person_id
    """))).all())

    changed, unchanged, unknown, refused = [], 0, [], []
    for person in people:
        try:
            said = await house.birth_date(config, person.name)
        except DidaError as exc:
            return {"error": str(exc), "asked": len(changed) + unchanged + len(unknown),
                    "changed": changed, "unchanged": unchanged,
                    "not_in_the_book": unknown, "contradicted": refused,
                    "written": False}
        if not said:
            unknown.append(person.name)
            continue
        when = said.get("born_on")
        if not when:
            unknown.append(person.name)
            continue
        already = person.born_on.isoformat() if person.born_on else None
        if already == when and person.born_source == "contacts":
            unchanged += 1
            continue
        first = earliest.get(person.id)
        said_on = datetime.date.fromisoformat(when)
        if first and said_on > first:
            # Which of the two is wrong is usually legible. A date whose day and
            # month match the earliest photograph and whose year does not is a
            # mistyped year — that is what a typo looks like. A date unrelated to
            # it means the photographs before it are of somebody else, which is
            # precisely the mistake a birth date is kept to catch: two babies of
            # the same age are alike to any recogniser.
            typo = (said_on.month, said_on.day) == (first.month, first.day)
            refused.append({
                "person": person.name, "book_says": when,
                "earliest_photograph": first.isoformat(), "held": already,
                "looks_like": "a mistyped year in the book" if typo
                              else "faces before that date that are not this person",
            })
            continue
        changed.append({"person": person.name, "id": person.id,
                        "was": already, "now": when})
        if write:
            person.born_on = datetime.date.fromisoformat(when)
            person.born_source = "contacts"
            person.contact_id = said.get("contact_id") or ""
    if write:
        await session.commit()
    return {"asked": len(people), "changed": changed, "unchanged": unchanged,
            "not_in_the_book": unknown, "contradicted": refused, "written": write}
