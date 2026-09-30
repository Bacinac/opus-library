import pytest

from opus.music.textnorm import answers


@pytest.mark.parametrize(("query", "name", "answered"), [
    ("adam semijalac", "Adam Semijalac", True),
    ("adam semijalac", "Jala Brat", False),
    ("adam semijalac", "Prljavo Kazaliste", False),
    ("semijalac", "Seka Aleksić", False),
    ("semijalac", "Specijalac", False),
    ("prljavo", "Prljavo Kazalište", True),
    ("prljavo kaz", "Prljavo Kazaliste", True),
    ("kazaliste", "Prljavo Kazalište", True),
    ("prljvo kazaliste", "Prljavo Kazalište", True),
    ("the beatles", "Beatles", True),
    ("jala brat mafija", "Mafija", True),
    ("", "Jala Brat", False),
    ("jala brat", "Japa No Beat", False),
    ("djordje balasevic", "Ђорђе Балашевић", True),
    ("Ђорђе Балашевић", "Đorđe Balašević", True),
])
def test_a_catalogue_hit_counts_only_when_it_answers_what_was_typed(query, name, answered):
    assert answers(query, name) is answered
