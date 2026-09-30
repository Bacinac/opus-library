"""Shared text normalization for fuzzy matching: lowercase, straighten curly
quotes, fold diacritics, punctuation to spaces. A filename's "Witch’s Spell"
must match the catalog's "Witch's Spell", and the tag "Arsen Dedić" must be
IDENTICAL to Deezer's ASCII "Arsen Dedic" — sources are inconsistent about
č/ć/š/ž/đ, so matching folds them (display never uses norm()). The
filesystem-name sanitiser lives here too: the tagger and a channel that
names the files it fetches must agree character for character on the name a track gets on disk."""

import re
import unicodedata

from rapidfuzz import fuzz

_QUOTE_MAP = {0x2019: "'", 0x2018: "'", 0x201C: '"', 0x201D: '"'}
# đ/Đ are standalone letters — NFKD does not decompose them
_DJ_MAP = {0x111: "d", 0x110: "d"}
# the underscore is \w to Python and a space to every scene ripper: without it
# folded, '02-the_who-its_a_boy.flac' matches none of the catalog's titles
_PUNCT = re.compile(r"[^\w\s]|_")
PARENS = re.compile(r"[\(\[][^)\]]*[\)\]]")


_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e",
    "ж": "ž", "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj",
    "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "ћ": "ć", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "č",
    "џ": "dž", "ш": "š",
}
_CYRILLIC.update({k.upper(): v.upper() for k, v in _CYRILLIC.items()})
_TO_LATIN = str.maketrans(_CYRILLIC)


def latin(text: str) -> str:
    """Serbian Cyrillic written the other way it is written. Half of ex-Yu
    music has its article on sr.wikipedia in Cyrillic while the catalog spells
    the same act in Latin, and the two must be able to meet."""
    return text.translate(_TO_LATIN)


def norm(text: str) -> str:
    text = text.lower().translate(_QUOTE_MAP).translate(_DJ_MAP)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _PUNCT.sub(" ", text)


def safe_filename(name: str) -> str:
    """The characters no filesystem takes in a path segment, replaced. Nothing
    is folded — this text is displayed, it names the file on disk."""
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def same_name(a: str, b: str) -> bool:
    """Folded exact equality — the 'exact match' notion for ranking."""
    return " ".join(norm(a).split()) == " ".join(norm(b).split())


def artist_key(name: str) -> str:
    """norm() plus a dropped leading English article: tags and catalogs
    disagree about the 'The' constantly ('Blues Brothers' vs 'The Blues
    Brothers' is the same act). Never strips a name to nothing ('The The')."""
    tokens = norm(name).split()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    return " ".join(tokens)


def same_artist(a: str, b: str) -> bool:
    """Exact artist identity: folded equality ignoring the leading article and
    token order ('Dedić, Arsen' tags vs the catalog's 'Arsen Dedić'). Word
    order is the ONLY freedom — a token added or dropped is a different act
    ('Brothers In Blues' is not 'Blues Brothers')."""
    ka, kb = artist_key(a), artist_key(b)
    return ka == kb or sorted(ka.split()) == sorted(kb.split())


def answers(query: str, name: str) -> bool:
    """Whether an artist a catalogue offered is an answer to what was typed.
    Every word typed begins a word of the name ('prljavo kaz'), the whole name
    was typed among other words, or the two differ by a slip of the keyboard.
    A catalogue that knows nobody by that name answers with whoever is popular —
    Spotify offers Jala Brat for a singer it does not carry — and that is not an
    answer."""
    asked, named = artist_key(latin(query)).split(), artist_key(latin(name)).split()
    if not asked or not named:
        return False
    if all(any(word.startswith(part) for word in named) for part in asked):
        return True
    if all(word in asked for word in named):
        return True
    return fuzz.ratio(" ".join(asked), " ".join(named)) >= 85


_SMALL_WORDS = {"i", "in", "of", "the", "and", "a", "na", "za", "und", "de"}


def clean_display_name(name: str) -> str:
    """Title-case a multi-word ALL-CAPS catalog name (BELFAST FOOD -> Belfast
    Food) that no editorial source bothered to case; leave punctuated acronyms
    (AC/DC, R.E.M., S.A.R.S.) and single tokens (U2) alone — they carry a '.'/'/'
    or no space at all."""
    s = name.strip()
    if " " not in s or s != s.upper() or any(c in s for c in "./"):
        return s
    return " ".join(
        w.lower() if i and w.lower() in _SMALL_WORDS else w[:1].upper() + w[1:].lower()
        for i, w in enumerate(s.split())
    )


def album_score(catalog_title: str, tag_album: str) -> float:
    """Album-title match for folder adoption. token_sort (not token_set: the
    set variant scores 100 for the single "Stockton Gala Days (Live on MTV
    Unplugged)" against the album "MTV Unplugged") plus qualifier-stripped
    variants on BOTH sides: the catalog's "(Deluxe Edition)" brackets and the
    tag's colon subtitle ("Only the Strong Survive: Covers, Vol. 1")."""
    catalog_variants = [catalog_title]
    stripped = PARENS.sub(" ", catalog_title)
    if stripped != catalog_title:
        catalog_variants.append(stripped)
    tag_variants = [tag_album]
    if ":" in tag_album:
        tag_variants.append(tag_album.split(":", 1)[0])
    return max(
        fuzz.token_sort_ratio(norm(c), norm(t))
        for c in catalog_variants
        for t in tag_variants
    )


# the score at or above which title_score() counts as the same title
TITLE_MATCH_THRESHOLD = 65


def title_score(title: str, name: str) -> float:
    """Best fuzzy score between a catalog title and a file/candidate name.
    Filenames usually drop parenthetical qualifiers — "Eat for Two (Live
    Unplugged)" must still match "eat for two" — so the stripped variant is
    scored too and the better result wins. Only for title-vs-file matching:
    catalog-vs-catalog comparison must NOT strip ("Album (Live)" != "Album")."""
    def pair(a: str, b: str) -> float:
        best = fuzz.token_set_ratio(a, b)
        # spacing differences ("Vondel Park" vs "vondelpark") defeat token
        # scoring: fold the title into one token, and despace both sides
        best = max(best, fuzz.token_set_ratio(a.replace(" ", ""), b))
        return max(best, fuzz.ratio(a.replace(" ", ""), b.replace(" ", "")))

    score = pair(norm(title), norm(name))
    stripped = PARENS.sub(" ", title)
    if stripped != title:
        score = max(score, pair(norm(stripped), norm(name)))
    return score
