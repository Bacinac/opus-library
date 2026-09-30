import datetime

import pytest

from conftest import library, run, signed_in
from opus import db
from opus.models import Person, Photo
from opus.photos.library import search
from scene import Scene

HVAR = ("Hvar", "HR")
VIENNA = ("Vienna", "AT")
POLJE = ("Veliko Polje", "HR")

WORDS = search.Vocabulary(
    people={"kata": frozenset({1}), "kata babic": frozenset({1}), "babic": frozenset({1, 6}),
            "marko": frozenset({2, 3}), "ivan": frozenset({4}), "eva": frozenset({5})},
    places={"hvar": frozenset({HVAR}), "vienna": frozenset({VIENNA}),
            "bec": frozenset({VIENNA}), "veliko polje": frozenset({POLJE})},
    countries={"italija": "IT", "italy": "IT", "hrvatska": "HR"},
)


def said(query: str) -> list[tuple[str, frozenset]]:
    return [(t.kind, t.value) for t in search.read(query, WORDS).terms]


def test_a_name_a_place_and_a_year_are_three_things_to_narrow_by():
    assert said("Kata Hvar 2019") == [
        ("person", frozenset({1})), ("place", frozenset({HVAR})), ("years", frozenset({2019}))]


def test_names_are_found_in_the_case_a_sentence_puts_them_in():
    assert said("s Katom na Hvaru u srpnju 2019.") == [
        ("person", frozenset({1})), ("place", frozenset({HVAR})),
        ("months", frozenset({7})), ("years", frozenset({2019}))]


def test_a_place_answers_to_what_it_is_called_here_and_without_its_accents():
    assert said("u Beču") == said("bec") == [("place", frozenset({VIENNA}))]


def test_a_longer_name_is_read_as_one_before_its_words_are_read_apart():
    assert said("Kata Babic") == [("person", frozenset({1}))]
    assert said("Veliko Polje") == said("na Velikom Polju") == [("place", frozenset({POLJE}))]


def test_a_given_name_several_people_share_is_any_of_them():
    assert said("Marko") == [("person", frozenset({2, 3}))]


def test_a_name_as_written_wins_over_another_name_in_some_case():
    """Eva is somebody, and also Ivan in the genitive."""
    assert said("Eva") == [("person", frozenset({5}))]


def test_a_country_is_named_in_either_language():
    assert said("Italija") == said("Italy") == [("country", frozenset({"IT"}))]
    assert said("u Italiji") == [("country", frozenset({"IT"}))]


def test_spans_of_years_and_seasons_are_dates_too():
    assert said("2015-2017") == [("years", frozenset({2015, 2016, 2017}))]
    assert said("ljeto") == [("months", frozenset({6, 7, 8}))]


def test_a_word_that_names_nothing_is_said_back_rather_than_guessed():
    reading = search.read("plaža Hvar", WORDS)
    assert reading.unread == ("plaža",)
    assert [t.kind for t in reading.terms] == ["place"]


def test_other_names_come_from_the_list_the_places_were_named_from(tmp_path):
    kept = tmp_path / "cities500.txt"
    row = ["2761369", "Vienna", "Vienna", "Bec,Beč,VIE,Wien", "48.2", "16.37", "P", "PPLC", "AT"]
    district = ["1", "Mitte", "Mitte", "Beč", "0", "0", "P", "PPLX", "AT"]
    kept.write_text("\n".join("\t".join(r + ["", ""]) for r in (row, district)) + "\n")
    names = search._alternates(kept, frozenset({VIENNA}))
    assert names == {VIENNA: {"Bec", "Beč", "Wien"}}


@pytest.fixture
def summers(clean):
    """Kata on Hvar in two summers, Marko in Vienna, and an empty beach."""
    scene = Scene()

    async def build():
        async with db.SessionLocal() as session:
            kata = Person(given_name="Kata", family_name="Babic", name="Kata Babic")
            marko = Person(given_name="Marko", family_name="Vuk", name="Marko Vuk")
            session.add_all([kata, marko])
            await session.flush()
            shot = {}
            for label, (year, month), (place, country), who in [
                ("kata19", (2019, 7), HVAR, kata), ("kata21", (2021, 7), HVAR, kata),
                ("marko", (2019, 12), VIENNA, marko), ("beach", (2019, 7), HVAR, None),
            ]:
                taken = datetime.datetime(year, month, 10, 12, tzinfo=datetime.UTC)
                photo = await session.get(Photo, await scene.photo(session, taken))
                photo.place, photo.country = place, country
                if who:
                    await scene.face(session, photo.id, scene.base(), label, person_id=who.id)
                shot[label] = photo.checksum.hex()
            await session.commit()
            return shot

    return run(build()), run(signed_in("boss"))


def test_the_shelf_and_its_rail_are_narrowed_by_what_was_read(summers):
    shot, token = summers

    async def ask():
        async with library(token) as client:
            found = (await client.get("/api/photos/timeline", params={"q": "Kata na Hvaru 2019"})).json()
            rail = (await client.get("/api/photos/timeline/buckets", params={"q": "Kata na Hvaru 2019"})).json()
            summer = (await client.get("/api/photos/timeline", params={"q": "Hvar ljeto"})).json()
            nothing = (await client.get("/api/photos/timeline", params={"q": "plaža"})).json()
            reading = (await client.get("/api/photos/search", params={"q": "Kata u Vienni plaža"})).json()
            return found, rail, summer, nothing, reading

    found, rail, summer, nothing, reading = run(ask())
    assert [p["id"] for p in found["photos"]] == [shot["kata19"]]
    assert rail["months"] == [{"month": "2019-07", "count": 1}]
    assert {p["id"] for p in summer["photos"]} == {shot["kata19"], shot["kata21"], shot["beach"]}
    assert nothing["photos"] == []
    assert reading == {
        "terms": [
            {"kind": "person", "said": "Kata", "people": [{"id": reading["terms"][0]["people"][0]["id"],
                                                         "name": "Kata Babic"}]},
            {"kind": "place", "said": "Vienni", "places": [{"place": "Vienna", "country": "AT"}]},
        ],
        "unread": ["plaža"],
    }
