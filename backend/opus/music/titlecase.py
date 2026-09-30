"""The form a title is shown in, and written in.

The catalogue arrives in every shape its sources keep it in: seven thousand
titles with one capital, four and a half thousand with one on every word, two
hundred shouted in full capitals. None of that is a decision anybody made about
this library — it is four services disagreeing.

What is correct depends on the language, and the language is in the title rather
than in the artist: Let 3 called a record `Two Dogs Fuckin'` and Dubioza
Kolektiv called one `Wild Wild East`, so asking where the band is from answers
the wrong question.

English titles take capitals on everything but the small joining words. Croatian
takes them on every word, because there the danger is real: `Leut Magnetik` is a
name and `Ostala Si Uvijek Ista` is a sentence, and nothing in the spelling tells
them apart. Adding a capital is never an error about a name; removing one is.
"""

import re

# what a title says with, rather than about — lowercase in an English title
# unless it opens or closes it
_MINOR_EN = {
    # verbs keep their capital in a title, `is` and `be` among them; these are
    # the joining words and nothing else. `up` and `off` are NOT among them:
    # in song titles they are almost always the particle of a phrasal verb —
    # `Back Off Bitch`, `Make Up Your Mind`, `Blow Up the Outside World` — and
    # of four hundred titles carrying one mid-sentence, none wanted it lowered
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "on", "onto", "or", "over", "the", "to", "with", "vs", "via",
}

# Croatian words that are not ALSO English ones. `to`, `on`, `me`, `sam`, `do`
# and `no` are in both languages, and letting any of them decide sent `Back To
# Black` and `An Open Letter To NYC` down the Croatian path.
_ONLY_HR = {
    "je", "su", "smo", "ste", "što", "koji", "koja", "koje", "kad", "kada",
    "gdje", "kako", "zašto", "jer", "sve", "svi", "sva", "ovaj", "ova", "ovo",
    "taj", "ta", "onaj", "moj", "moja", "moje", "tvoj", "tvoja", "tvoje",
    "naš", "naša", "naše", "njih", "nas", "vas", "bez", "kroz", "niz", "uz",
    "pod", "nad", "pred", "prema", "poslije", "prije", "ali", "ili", "jedan",
    "jedna", "jedno", "još", "već", "samo", "opet", "uvijek", "nikad",
    "nikada", "sada", "tada", "ovdje", "tamo", "malo", "puno", "više", "manje",
    "nešto", "ništa", "netko", "nitko", "svaki", "svaka", "svako", "ljubav",
    "srce", "život", "svijet", "noć", "dan", "vrijeme", "nije", "bio", "bila",
    "bilo", "ćeš", "ću", "će", "mi", "ti", "vi", "oni", "ona", "ono", "ga",
    "mu", "joj", "im", "za", "od", "sa", "pa", "te", "da", "li", "se", "ne",
}

_OPENS = re.compile(r"[\(\[\{]")
_CLOSES = re.compile(r"[\)\]\}]")
_HR_LETTERS = re.compile(r"[čćžšđČĆŽŠĐ]")
# an apostrophe is inside a word, not between two: splitting on it turns
# `Jezebel's Father` into `Jezebel'S Father`
_WORD = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]*)*", re.UNICODE)


def _english(title: str) -> bool:
    """Whether the minor words in this title are English ones. Croatian keeps
    its capitals on `za`, `i` and `u` — which is how this shelf already spells
    them — so the small-word rule is English-only."""
    if _HR_LETTERS.search(title):
        return False
    words = [w.lower() for w in _WORD.findall(title)]
    return sum(1 for w in words if w in _ONLY_HR) < 2


def _shouting(words: list[str]) -> list[bool]:
    """Which of these are capitals because somebody held the key down.

    A title of one word is never a shout: `ABBA` and `TBF` are how those are
    written, and nothing in the letters separates them from `THRILLER`. Past
    that, where EVERY word is capitals it is a shout; where only one run of
    them stands among ordinary words it is an abbreviation, and `CSNY / Deja
    Vu` names a band."""
    if len(words) < 2:
        return [False] * len(words)
    caps = [w.isupper() and w != w.lower() for w in words]
    if all(caps) and max(len(w) for w in words) > 3:
        return [True] * len(words)
    long_caps = [c and len(w) > 3 for w, c in zip(words, caps)]
    return [
        this and ((i and long_caps[i - 1]) or (i + 1 < len(words) and long_caps[i + 1]))
        for i, this in enumerate(long_caps)
    ]


def _keep(word: str) -> bool:
    """A word that is spelt the way it arrived. Capitals that are the name —
    `ABBA`, `TBF`, `AC`, `DC`, `NYC`, `CSNY` — and anything carrying a capital
    inside it, which is somebody's own spelling: `McCartney`, `iPhone`."""
    if word.isupper() and word != word.lower():
        return True
    return any(c.isupper() for c in word[1:])


def _cap(word: str) -> str:
    return word[:1].upper() + word[1:].lower() if word else word


def display_title(title: str) -> str:
    """The form this title should be shown and tagged in.

    Every word takes a capital. That is what this shelf already looks like — a
    thousand of its albums against eight hundred written as a sentence — and it
    is the direction that cannot be wrong: putting a capital on a word is never
    an error about a name, taking one off is. `Dolazi Božić` survives it and
    `Dolazi božić` does not, and no reading of the letters tells a machine which
    of the two `Božić` is.

    So nothing here lowers a word it does not recognise. Only the small joining
    words of an English title come down, and only between the first and the
    last."""
    text = (title or "").strip()
    if not text:
        return title
    # the apostrophe stays inside the word it belongs to
    parts = re.split(r"([^\w'\u2019]+|_+)", text)
    words = [i for i, part in enumerate(parts) if _WORD.fullmatch(part)]
    minor = _MINOR_EN if _english(text) else set()
    shouts = _shouting([parts[i] for i in words])
    out = list(parts)
    for n, i in enumerate(words):
        word = parts[i]
        if not shouts[n] and _keep(word):
            continue
        low = word.lower()
        # a bracket opens a title of its own: `(The Only One)` is not the middle
        # of a sentence, and neither is the word that closes one
        opens = i and _OPENS.search(parts[i - 1])
        closes = i + 1 < len(parts) and _CLOSES.search(parts[i + 1])
        first_or_last = n == 0 or n == len(words) - 1 or opens or closes
        out[i] = low if not first_or_last and low in minor else _cap(word)
    return "".join(out)
