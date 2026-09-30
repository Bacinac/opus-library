import datetime

from conftest import run
from opus.photos.people import lives

D = datetime.date


def row(cid, n, y1, y2, pid=None, pname=None, known=None, born=None):
    return (cid, n, y1, y2, D(y1, 3, 1) if y1 else None, D(y2, 9, 1) if y2 else None,
            cid * 10, pid, pname, known, born)


DETAIL = [
    row(1, 10, 2008, 2008, born=2007.8),
    row(2, 12, 2010, 2010, born=2008.2),
    row(3, 9, 2011, 2011, born=2007.9),
    row(4, 11, 2008, 2008, born=2005.0),
    row(5, 20, 2015, 2015, pid=100, pname="Ana"),
    row(6, 8, 2019, 2019, pid=100, pname="Ana"),
    row(7, 15, 2016, 2016, pid=200, pname="Iva"),
    row(8, 12, 2016, 2016),
    row(9, 9, 2016, 2018),
    row(10, 9, 2016, 2018),
    row(11, 30, 2002, 2002, pid=300, pname="Mia", known=D(2000, 5, 1)),
    row(12, 14, 2003, 2003, born=2001.0),
    row(13, 3, 2003, 2003, born=2001.2),
    row(14, 8, None, None),
]
# a, b, likeness, and how far down each other's list of nearest they sit
EDGES = [
    (6, 7, 0.80, 1), (12, 13, 0.90, 1), (2, 3, 0.70, 1), (1, 4, 0.67, 1),
    (1, 2, 0.58, 3), (7, 8, 0.50, 1), (9, 10, 0.50, 2), (11, 12, 0.50, 5),
]


class Answer:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def one(self):
        return self.rows


class Catalogue:
    def __init__(self, shape=(1,)):
        self.shape = shape
        self.asked = 0

    async def execute(self, *args, **kwargs):
        self.asked += 1
        return Answer({1: self.shape, 2: DETAIL, 3: EDGES}[(self.asked - 1) % 3 + 1])


def test_lives_are_assembled_by_name_likeness_and_birth(clean):
    found = run(lives.runs(Catalogue()))
    assert [(life["person"] or {}).get("name") for life in found] == ["Mia", "Ana", "Iva", None]
    assert [[(era["year"], era["ids"], era["id"], era["faces"], era["cover"]) for era in life["groups"]]
            for life in found] == [
        [(2002, [11], 11, 30, 110), (2003, [12, 13], 12, 17, 120)],
        [(2015, [5], 5, 20, 50), (2019, [6], 6, 8, 60)],
        [(2016, [7, 8], 7, 27, 70)],
        [(2008, [1], 1, 10, 10), (2010, [2], 2, 12, 20), (2011, [3], 3, 9, 30)],
    ]
    assert [[c["id"] for c in life["clusters"]] for life in found] == [
        [11, 12, 13], [5, 6], [7, 8], [1, 2, 3]]
    assert [(life["faces"], life["years"], life["first"], life["last"], life["born"],
             life["certain"]) for life in found] == [
        (47, [2002, 2003], "2002-03-01", "2003-09-01", 2000, True),
        (28, [2015, 2019], "2015-03-01", "2019-09-01", None, False),
        (27, [2016, 2016], "2016-03-01", "2016-09-01", None, False),
        (31, [2008, 2011], "2008-03-01", "2011-09-01", 2008, False),
    ]
    assert found[2]["groups"][0]["person"] == {"id": 200, "name": "Iva"}


def test_the_answer_is_kept_until_the_shape_changes(clean):
    first = Catalogue()
    kept = run(lives.runs(first))
    again = Catalogue()
    assert run(lives.runs(again)) is kept and again.asked == 1
    moved = Catalogue(shape=(2,))
    assert run(lives.runs(moved)) is not kept and moved.asked == 3
