import datetime

import pytest
from sqlalchemy import select

from conftest import library, run, signed_in
from opus import db
from opus.models import Person, User
from scene import Scene


@pytest.fixture
def one_day(clean):
    """One day of three years: the household in one, a stranger in another, and
    a photograph with nobody in it."""
    scene = Scene()

    async def build():
        async with db.SessionLocal() as session:
            filip = Person(given_name="Filip", family_name="Babic", name="Filip Babic")
            guest = Person(given_name="Ana", family_name="Vuk", name="Ana Vuk")
            session.add_all([filip, guest])
            await session.flush()
            day = datetime.datetime(2013, 9, 22, 12, tzinfo=datetime.UTC)
            ours = await scene.photo(session, day)
            await scene.face(session, ours, scene.base(), "filip", person_id=filip.id)
            theirs = await scene.photo(session, day.replace(year=2011))
            await scene.face(session, theirs, scene.base(), "ana", person_id=guest.id)
            await scene.photo(session, day.replace(year=2009))
            await session.commit()
            return filip.id

    person_id = run(build())

    async def link():
        async with db.SessionLocal() as session:
            token = await signed_in("filip", role="user")
            user = (await session.execute(
                select(User).where(User.name == "filip"))).scalar_one()
            user.person_id = person_id
            await session.commit()
            return token

    return run(link())


def test_a_thin_day_reaches_for_the_days_around_it_when_a_screen_must_be_filled(one_day):
    """Three photographs on the day itself, five scattered over a fortnight
    either side of it: a wall that shows one every twenty seconds is given the
    nearest days rather than the same three all evening."""

    async def spread():
        async with db.SessionLocal() as session:
            scene = Scene()
            scene.photos = 100
            for off in (-12, -9, -2, 6, 11):
                when = datetime.datetime(2016, 9, 22, 12, tzinfo=datetime.UTC) \
                    + datetime.timedelta(days=off)
                await scene.photo(session, when)
            await session.commit()

    run(spread())

    async def scenario():
        async with library(one_day) as client:
            day = await client.get("/api/photos/onthisday?month=9&day=22")
            wall = await client.get("/api/photos/onthisday?month=9&day=22&least=8")
            return day.json(), wall.json()

    day, wall = run(scenario())
    # left alone it stays an anniversary: three days at the most
    assert (day["span"], day["total"]) == (0, 3)
    assert wall["span"] == 14 and wall["total"] == 8
    assert 2016 in [y["year"] for y in wall["years"]]


def test_the_screensaver_is_shown_the_household_and_nobody_else(one_day):
    async def scenario():
        async with library(one_day) as client:
            whole = await client.get("/api/photos/onthisday?month=9&day=22&span=0")
            ours = await client.get(
                "/api/photos/onthisday?month=9&day=22&span=0&household=true")
            shelf = await client.get("/api/photos/timeline?household=true")
            return whole.json(), ours.json(), shelf.json()

    whole, ours, shelf = run(scenario())
    assert [y["year"] for y in whole["years"]] == [2013, 2011, 2009]
    # the stranger's year is gone; the photograph of nobody stays, because a
    # landscape is nobody's privacy
    assert [y["year"] for y in ours["years"]] == [2013, 2009]
    assert len(shelf["photos"]) == 2


def test_this_day_is_narrowed_to_somebody_the_way_every_screen_is(one_day):
    """A profile's screensaver shows this day of the people it chose, or of the
    family, by the same narrowing the years and the shelf take."""

    async def scenario():
        async with library(one_day) as client:
            people = {p["name"]: p["id"] for p in (await client.get("/api/photos/people")).json()}
            day = "/api/photos/onthisday?month=9&day=22&span=0"
            his = await client.get(f"{day}&person={people['Filip Babic']}")
            hers = await client.get(f"{day}&person={people['Ana Vuk']}")
            both = await client.get(f"{day}&person={people['Filip Babic']}&person={people['Ana Vuk']}")
            ours = await client.get(f"{day}&family=true")
            shelf = await client.get(f"/api/photos/timeline?person={people['Ana Vuk']}")
            rail = await client.get("/api/photos/timeline/buckets?family=true")
            return [r.json() for r in (his, hers, both, ours, shelf, rail)]

    his, hers, both, ours, shelf, rail = run(scenario())
    assert [y["year"] for y in his["years"]] == [2013]
    assert [y["year"] for y in hers["years"]] == [2011]
    assert sorted(y["year"] for y in both["years"]) == [2011, 2013]
    # the family is somebody of it in the picture: the landscape is the
    # household's, not the family's
    assert [y["year"] for y in ours["years"]] == [2013]
    assert len(shelf["photos"]) == 1
    assert rail["months"] == [{"month": "2013-09", "count": 1}]


def test_a_photograph_is_captioned_with_who_is_in_it_not_who_the_year_was_about(one_day):
    """The screensaver shows one photograph at a time under its year: the emu
    Filip walked past that afternoon is not a photograph of Filip."""

    async def nobody():
        async with db.SessionLocal() as session:
            scene = Scene()
            scene.photos = 100
            await scene.photo(session, datetime.datetime(2013, 9, 22, 15, tzinfo=datetime.UTC))
            await session.commit()

    run(nobody())

    async def scenario():
        async with library(one_day) as client:
            return (await client.get("/api/photos/onthisday?month=9&day=22&span=0")).json()

    year = next(y for y in run(scenario())["years"] if y["year"] == 2013)
    assert [one["name"] for one in year["people"]] == ["Filip"]
    assert sorted(p["people"] for p in year["photographs"]) == [[], ["Filip"]]


def test_family_without_an_account_is_marked_and_the_account_holder_already_is(one_day):
    async def scenario():
        async with library(one_day) as client:
            people = {p["name"]: p for p in (await client.get("/api/photos/people")).json()}
            ana = people["Ana Vuk"]
            refused = await client.put(f"/api/photos/people/{ana['id']}", json={"family": True})
        async with library(await signed_in("boss")) as admin:
            marked = await admin.put(f"/api/photos/people/{ana['id']}", json={"family": True})
        async with library(one_day) as client:
            wall = await client.get(
                "/api/photos/onthisday?month=9&day=22&span=0&household=true")
            again = {p["name"]: p for p in (await client.get("/api/photos/people")).json()}
            return people, refused, marked, wall.json(), again

    people, refused, marked, wall, again = run(scenario())
    # who counts as family is the household's to say, not every member's
    assert refused.status_code == 403
    assert (people["Filip Babic"]["family"], people["Filip Babic"]["roster"]) == (True, True)
    assert (people["Ana Vuk"]["family"], people["Ana Vuk"]["roster"]) == (False, False)
    assert (people["Filip Babic"]["account"], people["Ana Vuk"]["account"]) == ("filip", None)
    assert marked.status_code == 200 and marked.json()["family"] is True
    assert [y["year"] for y in wall["years"]] == [2013, 2011, 2009]
    assert again["Ana Vuk"]["family"] is True


def test_a_person_and_the_family_are_drawn_from_every_year_they_are_in(one_day):
    """The television asked for Filip shows a handful out of each of his years,
    and asked for the family shows nobody else's and no empty landscapes."""

    async def more():
        async with db.SessionLocal() as session:
            scene = Scene()
            scene.photos = 100
            filip = (await session.execute(
                select(Person).where(Person.given_name == "Filip"))).scalar_one()
            for when in (datetime.datetime(2013, 3, 1, tzinfo=datetime.UTC),
                         datetime.datetime(2013, 5, 1, tzinfo=datetime.UTC),
                         datetime.datetime(2010, 7, 1, tzinfo=datetime.UTC)):
                photo = await scene.photo(session, when)
                await scene.face(session, photo, scene.base(), "filip", person_id=filip.id)
            await session.commit()
            return filip.id

    filip = run(more())

    async def scenario():
        async with library(one_day) as client:
            his = await client.get(f"/api/photos/years?person={filip}&each=2")
            ours = await client.get("/api/photos/years?family=true")
            neither = await client.get("/api/photos/years")
            return his.json(), ours.json(), neither.status_code

    his, ours, neither = run(scenario())
    assert [(y["year"], len(y["photographs"])) for y in his["years"]] == [(2013, 2), (2010, 1)]
    assert all(p["people"] == ["Filip"] for y in his["years"] for p in y["photographs"])
    assert [(y["year"], len(y["photographs"])) for y in ours["years"]] == [(2013, 3), (2010, 1)]
    assert neither == 400
