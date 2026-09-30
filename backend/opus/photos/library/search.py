"""Photographs found by who is in them, where and when they were taken.

Not by what is in them: a model reading "beach" off a picture answers a
Croatian question with an English guess, and the one that would not needs
memory this box does not have. What the catalogue already knows is how people
remember a photograph anyway — Kata, on Hvar, the summer of 2019.
"""

import asyncio
import dataclasses
import pathlib
import re
import unicodedata

from babel import Locale
from sqlalchemy import extract, false, func, or_, select, tuple_

from opus.models import Face, Person, Photo

# the longest name read as one, in words: Città del Vaticano
LONGEST = 3

QUIET = frozenset("""
    i u na s sa iz od do te ili a
    and in at on with of from or the
    godina godine godini godinu year
""".split())

MONTHS = {
    word: month
    for month, words in enumerate([
        "sijecanj sijecnja sijecnju january jan",
        "veljaca veljace veljaci february feb",
        "ozujak ozujka ozujku march mar",
        "travanj travnja travnju april apr",
        "svibanj svibnja svibnju may",
        "lipanj lipnja lipnju june jun",
        "srpanj srpnja srpnju july jul",
        "kolovoz kolovoza kolovozu august aug",
        "rujan rujna rujnu september sep sept",
        "listopad listopada listopadu october oct",
        "studeni studenog studenoga studenom studenome november nov",
        "prosinac prosinca prosincu december dec",
    ], start=1)
    for word in words.split()
}

SEASONS = {
    word: months
    for months, words in [
        ((3, 4, 5), "proljece proljeca proljecu spring"),
        ((6, 7, 8), "ljeto ljeta ljetu summer"),
        ((9, 10, 11), "jesen jeseni autumn fall"),
        ((12, 1, 2), "zima zime zimi zimu winter"),
    ]
    for word in words.split()
}

# What a Croatian name turns into in a sentence: na Hvaru, u Beču, s Katom.
ENDINGS = ("a", "e", "i", "u", "o", "om", "em", "oj", "ima", "ama")

WORD = re.compile(r"\d{4}\s*[-–]\s*\d{4}|\w+")
SPAN = re.compile(r"(\d{4})\s*[-–]\s*(\d{4})")


def fold(text: str) -> str:
    """Case, diacritics and punctuation away: typed on a phone, Beč is bec."""
    bare = unicodedata.normalize("NFKD", text.casefold().replace("đ", "d"))
    bare = "".join(c for c in bare if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", bare).split())


def _inflected(typed: str, name: str) -> bool:
    if typed == name:
        return True
    if len(name) < 3:
        return False
    stem = name[:-1] if name[-1] in "aeiou" else name
    return typed.startswith(stem) and typed[len(stem):] in ENDINGS


@dataclasses.dataclass(frozen=True)
class Vocabulary:
    """Every name the catalogue answers to, folded."""
    people: dict[str, frozenset[int]]
    places: dict[str, frozenset[tuple[str, str]]]
    countries: dict[str, str]


@dataclasses.dataclass(frozen=True)
class Term:
    kind: str
    said: str
    value: frozenset


@dataclasses.dataclass(frozen=True)
class Reading:
    terms: tuple[Term, ...]
    unread: tuple[str, ...]


def _when(word: str) -> Term | None:
    span = SPAN.fullmatch(word)
    if span:
        first, last = sorted(int(y) for y in span.groups())
        return Term("years", word, frozenset(range(first, last + 1)))
    if re.fullmatch(r"(19|20)\d\d", word):
        return Term("years", word, frozenset({int(word)}))
    folded = fold(word)
    if folded in MONTHS:
        return Term("months", word, frozenset({MONTHS[folded]}))
    if folded in SEASONS:
        return Term("months", word, frozenset(SEASONS[folded]))
    return None


def _named(words: list[str], vocabulary: Vocabulary, exact: bool) -> Term | None:
    said = " ".join(words)
    typed = fold(said).split()
    for kind, names in (("person", vocabulary.people), ("place", vocabulary.places),
                        ("country", vocabulary.countries)):
        if exact:
            found = names.get(" ".join(typed))
            if found is not None:
                return Term(kind, said, found if kind != "country" else frozenset({found}))
            continue
        hits = [found for name, found in names.items()
                if name[:2] == typed[0][:2] and len(name.split()) == len(typed)
                and all(_inflected(t, n) for t, n in zip(typed, name.split()))]
        if hits:
            if kind == "country":
                return Term(kind, said, frozenset(hits))
            return Term(kind, said, frozenset().union(*hits))
    return None


def read(query: str, vocabulary: Vocabulary) -> Reading:
    """What each word of a query names. Longer names are tried first, so
    Veliko Polje is one place and not a size and a field; a name as written
    comes before the same name in another case."""
    words = WORD.findall(query)
    terms: list[Term] = []
    unread: list[str] = []
    at = 0
    while at < len(words):
        word = words[at]
        if fold(word) in QUIET:
            at += 1
            continue
        when = _when(word)
        if when:
            terms.append(when)
            at += 1
            continue
        term = None
        for exact in (True, False):
            for size in range(min(LONGEST, len(words) - at), 0, -1):
                term = _named(words[at:at + size], vocabulary, exact)
                if term:
                    break
            if term:
                break
        if term:
            terms.append(term)
            at += len(term.said.split())
        else:
            unread.append(word)
            at += 1
    return Reading(tuple(terms), tuple(unread))


def narrowed(query, reading: Reading, zone: str):
    """Everyone named is in it; it was taken at any of the places named, in any
    of the years named, in any of the months named. A query nothing of which
    could be read finds nothing, not the whole shelf."""
    if not reading.terms:
        return query.where(false())

    def of(kind: str) -> list[frozenset]:
        return [t.value for t in reading.terms if t.kind == kind]

    for who in of("person"):
        query = query.where(select(Face.id).where(
            Face.photo_id == Photo.id, Face.person_id.in_(who)).exists())
    places = frozenset().union(*of("place"))
    countries = frozenset().union(*of("country"))
    where = []
    if places:
        where.append(tuple_(Photo.place, Photo.country).in_(sorted(places)))
    if countries:
        where.append(Photo.country.in_(sorted(countries)))
    if where:
        query = query.where(or_(*where))
    local = func.timezone(zone, Photo.taken_at)
    years = frozenset().union(*of("years"))
    if years:
        query = query.where(extract("year", local).in_(sorted(years)))
    months = frozenset().union(*of("months"))
    if months:
        query = query.where(extract("month", local).in_(sorted(months)))
    return query


_known_elsewhere: tuple[tuple, dict] | None = None


def _alternates(kept: pathlib.Path, places: frozenset[tuple[str, str]]) -> dict:
    """Every other name GeoNames knows each of these places by — Beč for
    Vienna, Firenca for Florence. Read from the list the places were named
    from, matched the way they were named, and kept until the list or the
    places change."""
    global _known_elsewhere
    key = (kept.stat().st_mtime_ns, places)
    if _known_elsewhere and _known_elsewhere[0] == key:
        return _known_elsewhere[1]
    names: dict[tuple[str, str], set[str]] = {}
    with kept.open(encoding="utf-8") as lines:
        for line in lines:
            bits = line.split("\t")
            if len(bits) < 9 or bits[7] == "PPLX" or (bits[1], bits[8]) not in places:
                continue
            # an airport's code is not a name anybody searches a holiday by
            names.setdefault((bits[1], bits[8]), set()).update(
                a for a in bits[3].split(",") if a and not (len(a) <= 3 and a.isupper()))
    _known_elsewhere = (key, names)
    return names


async def vocabulary(session, root: pathlib.Path) -> Vocabulary:
    people: dict[str, set[int]] = {}
    for id_, name, given, family in (await session.execute(
            select(Person.id, Person.name, Person.given_name, Person.family_name))).all():
        for said in {name, given, family}:
            if fold(said):
                people.setdefault(fold(said), set()).add(id_)

    stored = frozenset((await session.execute(
        select(Photo.place, Photo.country).where(Photo.place != "").distinct())).all())
    kept = root / "places" / "cities500.txt"
    elsewhere = await asyncio.to_thread(_alternates, kept, stored) if kept.exists() else {}
    places: dict[str, set[tuple[str, str]]] = {}
    for place in stored:
        for said in {place[0], *elsewhere.get(place, ())}:
            if fold(said):
                places.setdefault(fold(said), set()).add(place)

    codes = (await session.execute(
        select(Photo.country).where(Photo.country != "").distinct())).scalars().all()
    countries = {}
    for language in ("hr", "en"):
        territories = Locale(language).territories
        for code in codes:
            if code in territories:
                countries[fold(territories[code])] = code

    return Vocabulary({k: frozenset(v) for k, v in people.items()},
                      {k: frozenset(v) for k, v in places.items()}, countries)
