"""Release categorization: studio | live | compilation | ep | single.
Deezer's record_type is the base signal, but plenty of best-of and live
releases are typed plain "album" (and Discogs masters carry no type at all),
so title patterns — English and Croatian — refine it. Derived on read, never
stored: reclassification applies everywhere instantly."""

import re

COMPILATION_PATTERNS = re.compile(
    r"\b(best of|the best|greatest hits|very best|anthology|antologija|"
    r"collection|kolekcija|hitovi|zlatna|zlatne|zlatni|najljepše|najveće|"
    r"platinum|singles|singlovi|gold|sampler|box set|boxset|classic albums|"
    r"original album|soundtrack|motion picture|hits|number ones)\b"
)
LIVE_PATTERNS = re.compile(r"\b(live|uživo|unplugged|koncert|koncertu|concert)\b")

# What a title says after the record's own name: "(Deluxe Edition)",
# "(2021 Remaster)", "(Extended Version)". Only a trailing bracket, because that
# is where a label puts it and because a bracket in the middle is usually part
# of the name.
EDITION_SUFFIX = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$")


def base_title(title: str) -> str:
    """The record a title names, with whichever pressing of it this happens to
    be left off."""
    return EDITION_SUFFIX.sub("", title or "").strip().casefold()


# What a bracket has to say for the title to be a pressing of something rather
# than a record of its own. Narrow on purpose: `(Live)` and `(Acoustic)` are not
# here, because a live album named after a studio one is a different record and
# must not be allowed to answer to its name.
EDITION_WORDS = re.compile(
    r"\b(remaster(?:ed)?|remastered|deluxe|expanded|extended|anniversary|"
    r"reissue|edition|version|mix|mono|stereo)\b", re.IGNORECASE)


def pressing_of(title: str) -> str | None:
    """The record this title is a pressing of, or None if it is a record.

    `Killers (2015 Remaster)` is a pressing of `Killers`; `Killers` is not a
    pressing of anything, and neither is `Powerslave (Live)`."""
    without = EDITION_SUFFIX.sub("", title or "").strip()
    whole = (title or "").strip()
    if not without or without == whole:
        return None
    return without if EDITION_WORDS.search(whole[len(without):]) else None


def editions(rows: list[tuple[int, str, str | None, int]]) -> dict[int, tuple[bool, str, str | None]]:
    """Which pressing stands for each record, and what the record itself is
    called and dated.

    `(id, title, release_date, files)` in; `{id: (stands, title, date)}` out.

    A remaster is not a second record and must not be a second line on a shelf.
    One pressing of each stands for it — the one with the most of the music,
    because that is the one somebody actually plays — and the rest step behind
    it. But the pressing that holds the files is the wrong place to read the
    record's name and year from: `Kill 'Em All (Remastered)` is dated 2016 and
    the record is from 1983, and a shelf ordered by what the pressing says runs
    a band's history backwards. So the name and the date are read off the
    plainest, earliest member of the group and handed to whichever pressing
    stands for it."""
    groups: dict[str, list[tuple[int, str, str | None, int]]] = {}
    for row in rows:
        groups.setdefault(base_title(row[1]), []).append(row)
    answer: dict[int, tuple[bool, str, str | None]] = {}
    for group in groups.values():
        # what the record is: the plainest name, and the earliest date anybody
        # credits it with
        named = min(group, key=lambda r: (len(r[1]), r[2] or "9999"))
        dated = min((r[2] for r in group if r[2]), default=None)
        # what stands for it: the most of the music, then the plainest name
        stands = max(group, key=lambda r: (r[3], -len(r[1])))
        for row in group:
            answer[row[0]] = (row is stands, named[1], dated)
    return answer


def classify_release(title: str, record_type: str | None) -> str:
    if record_type == "single":
        return "single"
    if record_type == "ep":
        return "ep"
    if record_type == "live":
        return "live"
    if record_type == "video":
        return "video"
    lowered = title.lower()
    if LIVE_PATTERNS.search(lowered):
        return "live"
    if record_type == "compilation" or COMPILATION_PATTERNS.search(lowered):
        return "compilation"
    return "studio"
