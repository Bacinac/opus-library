import pytest
from opus_core import dida

from conftest import run
from opus.photos.people import house
from opus.settings_store import RuntimeConfig

CONFIG = RuntimeConfig({"dida_url": "http://dida.test"})


@pytest.fixture
def book(monkeypatch):
    asked = []

    async def call(config, method, path, *, params=None, json=None):
        asked.append((method, path, params))
        if params.get("name") == "Nobody" or path.endswith("/birthdays"):
            raise dida.Absent(f"dida {path} failed: 404")
        return {"born_on": "2010-06-15"}

    monkeypatch.setattr(dida, "call", call)
    return asked


def test_the_book_is_asked_by_name(book):
    assert run(house.birth_date(CONFIG, "Kata")) == {"born_on": "2010-06-15"}
    assert book == [("GET", "/api/contacts/person", {"name": "Kata"})]


def test_a_name_the_book_does_not_hold_is_nobody(book):
    assert run(house.birth_date(CONFIG, "Nobody")) is None


def test_a_house_without_birthdays_is_an_error_not_an_empty_week(book):
    with pytest.raises(dida.DidaError):
        run(house.birthdays(CONFIG))
