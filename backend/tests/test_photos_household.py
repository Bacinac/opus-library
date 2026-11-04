import datetime
import hashlib

import opus_auth
import pytest
from sqlalchemy import select

from conftest import library, run, signed_in
from opus import auth, db
from opus.models import Device, Person, Setting, User
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


def _photo(n: int) -> str:
    return hashlib.sha1(str(n).encode()).hexdigest()


def _cousin():
    async def make():
        token = await signed_in("cousin", role="guest")
        async with db.SessionLocal() as session:
            ana = (await session.execute(select(Person).where(Person.name == "Ana Vuk"))).scalar_one()
            user = (await session.execute(select(User).where(User.name == "cousin"))).scalar_one()
            user.circle = [ana.id]
            await session.commit()
        auth.forget_roster()
        return token, ana.id
    return run(make())


def test_a_cousin_sees_the_photographs_his_circle_is_in_and_no_others(one_day):
    """Ana is in 2011, Filip in 2013, nobody in 2009. A guest whose circle is Ana
    sees 2011 on every screen, and the other two do not exist for him."""
    token, ana = _cousin()

    async def scenario():
        async with library(token) as client:
            return {
                "shelf": (await client.get("/api/photos/timeline")).json(),
                "rail": (await client.get("/api/photos/timeline/buckets")).json(),
                "day": (await client.get(
                    "/api/photos/onthisday?month=9&day=22&span=0&household=true")).json(),
                "years": (await client.get("/api/photos/years?family=true")).json(),
                "people": (await client.get("/api/photos/people")).json(),
                "search": (await client.get("/api/photos/search?q=Filip")).json(),
                "theirs": await client.get(f"/api/photos/{_photo(2)}"),
                "ours": await client.get(f"/api/photos/{_photo(1)}"),
                "nobody": await client.get(f"/api/photos/{_photo(3)}/preview"),
                "vault": await client.get("/api/photos/vault"),
            }

    seen = run(scenario())
    assert [p["id"] for p in seen["shelf"]["photos"]] == [_photo(2)]
    assert seen["rail"]["total"] == 1
    # the household and the family are the circle for him, not the house's
    assert [y["year"] for y in seen["day"]["years"]] == [2011]
    assert [y["year"] for y in seen["years"]["years"]] == [2011]
    assert [p["id"] for p in seen["people"]] == [ana]
    # Filip is not a name his search knows
    assert seen["search"]["unread"] == ["Filip"]
    assert seen["theirs"].status_code == 200 and seen["theirs"].json()["files"] == []
    assert seen["ours"].status_code == 404
    assert seen["nobody"].status_code == 404
    assert seen["vault"].status_code == 403


def test_the_player_asking_for_the_cousin_gets_what_he_would(one_day):
    token, _ = _cousin()

    async def scenario():
        async with db.SessionLocal() as session:
            session.add(Setting(key=auth.token_key("player"), value="player-token-0123456789"))
            await session.commit()
        from opus import settings_store
        settings_store.forget_runtime()
        async with library() as client:
            client.headers[opus_auth.TOKEN_HEADER] = "player-token-0123456789"
            whole = (await client.get("/api/photos/timeline")).json()
            client.headers[opus_auth.FOR_HEADER] = "cousin"
            his = (await client.get("/api/photos/timeline")).json()
            ours = await client.get(f"/api/photos/{_photo(1)}/preview")
            game = await client.get("/api/photos/draw")
            client.headers[opus_auth.FOR_HEADER] = "nobody"
            stranger = await client.get("/api/photos/timeline")
            return whole, his, ours, game, stranger

    whole, his, ours, game, stranger = run(scenario())
    assert len(whole["photos"]) == 3
    assert [p["id"] for p in his["photos"]] == [_photo(2)]
    assert ours.status_code == 404
    assert game.status_code == 403
    assert stranger.status_code == 403


def test_a_box_that_is_somebody_s_says_whose_and_leaves_with_them(one_day):
    _cousin()
    admin = run(signed_in("boss"))

    async def let_in():
        async with db.SessionLocal() as session:
            box = Device(module="opus-player", name="Cousin's", token="d" * 64,
                         let_in_at=datetime.datetime.now(datetime.UTC))
            session.add(box)
            await session.commit()
            return box.id

    box = run(let_in())

    async def scenario():
        async with library(admin) as client:
            refused = await client.patch(f"/api/auth/devices/{box}", json={"person": "boss"})
            his = await client.patch(f"/api/auth/devices/{box}", json={"person": "cousin"})
            listed = (await client.get("/api/auth/devices")).json()["devices"]
            back = await client.patch(f"/api/auth/devices/{box}", json={"person": None})
            again = await client.patch(f"/api/auth/devices/{box}", json={"person": "cousin"})
            gone = await client.delete("/api/auth/people/cousin")
        async with db.SessionLocal() as session:
            return refused, his, listed, back, again, gone, await session.get(Device, box)

    refused, his, listed, back, again, gone, kept = run(scenario())
    assert refused.status_code == 409
    assert his.json()["person"] == "cousin" and listed[0]["person"] == "cousin"
    assert back.json()["person"] is None and back.json()["name"] == "Cousin's"
    assert again.json()["person"] == "cousin"
    assert gone.status_code in (200, 204)
    # his box went with him rather than becoming the household's
    assert kept is None


def test_a_circle_is_set_by_an_admin_out_of_the_people_in_the_photographs(one_day):
    _cousin()
    admin = run(signed_in("boss"))

    async def scenario():
        async with library(admin) as client:
            people = {p["name"]: p["id"] for p in (await client.get("/api/photos/people")).json()}
            set_ = await client.patch("/api/auth/people/cousin",
                                      json={"circle": [people["Filip Babic"], people["Ana Vuk"]]})
            unknown = await client.patch("/api/auth/people/cousin", json={"circle": [987654]})
            roster = {p["name"]: p for p in (await client.get("/api/auth/people")).json()["people"]}
            return people, set_, unknown, roster

    people, set_, unknown, roster = run(scenario())
    assert set_.status_code == 200
    assert set_.json()["circle"] == sorted([people["Filip Babic"], people["Ana Vuk"]])
    assert unknown.status_code == 404
    assert roster["cousin"]["circle"] == sorted([people["Filip Babic"], people["Ana Vuk"]])
