"""Wikipedia wikitext parsing — pure text in, structured titles out. Article
markup is the only place that carries a hand-curated tracklist, the studio
discography list and the infobox album type, and none of it is machine-clean:
templates nest, titles hide behind [[links]], and the same section is written
three different ways across articles. No network, no state."""

import re

_TRACKLIST_TEMPLATE = re.compile(r"\{\{\s*track ?list(?:ing)?\s*(?=[|}])", re.IGNORECASE)
_TITLE_PARAM = re.compile(r"\s*title(\d+)\s*")
_HEADING = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)
_TRACK_HEADING = re.compile(r"(?:track ?listing|popis pjesama|popis skladbi)\b", re.IGNORECASE)
_WIKILINK = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]*)\]\]")
_LANG_TEMPLATE = re.compile(r"\{\{lang\|[^|{}]*\|([^{}]*?)\}\}", re.IGNORECASE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_TAG = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
_TRAILING_DURATION = re.compile(r"\s*\(\d{1,2}:\d{2}\)\s*$")


def _clean_wiki_title(raw: str) -> str:
    text = _HTML_COMMENT.sub("", raw)
    text = _REF_TAG.sub("", text)
    text = _LANG_TEMPLATE.sub(r"\1", text)
    text = _WIKILINK.sub(r"\1", text)
    text = text.replace("'''", "").replace("''", "").strip()
    if len(text) >= 2 and text[0] in '"“' and text[-1] in '"”':
        text = text[1:-1].strip()
    return text


def _split_template_params(body: str) -> list[str]:
    """Split on top-level | only — parameter values nest [[links|labels]] and
    {{templates|args}} whose pipes are not separators."""
    parts: list[str] = []
    current: list[str] = []
    braces = brackets = 0
    i = 0
    while i < len(body):
        pair = body[i:i + 2]
        if pair in ("{{", "}}", "[[", "]]"):
            if pair == "{{":
                braces += 1
            elif pair == "}}":
                braces -= 1
            elif pair == "[[":
                brackets += 1
            else:
                brackets -= 1
            current.append(pair)
            i += 2
            continue
        ch = body[i]
        if ch == "|" and braces == 0 and brackets == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _tracklist_template_bodies(wikitext: str) -> list[str]:
    bodies = []
    for match in _TRACKLIST_TEMPLATE.finditer(wikitext):
        depth, i = 0, match.start()
        while i < len(wikitext) - 1:
            pair = wikitext[i:i + 2]
            if pair == "{{":
                depth += 1
                i += 2
            elif pair == "}}":
                depth -= 1
                i += 2
                if depth == 0:
                    bodies.append(wikitext[match.start() + 2:i - 2])
                    break
            else:
                i += 1
    return bodies


def _template_titles(body: str) -> list[str]:
    numbered: dict[int, str] = {}
    for param in _split_template_params(body)[1:]:
        if "=" not in param:
            continue
        key, value = param.split("=", 1)
        match = _TITLE_PARAM.fullmatch(key)
        if match is None:
            continue
        title = _clean_wiki_title(value)
        if title:
            numbered[int(match.group(1))] = title
    return [numbered[n] for n in sorted(numbered)]


def _title_from_list_line(line: str) -> str | None:
    text = line.lstrip("#").strip()
    if not text:
        return None
    for pattern in (r'^["“](.+?)["”]', r"^''(.+?)''", r"^(\[\[.+?\]\])"):
        match = re.match(pattern, text)
        if match:
            text = match.group(1)
            break
    else:
        text = re.split(r"\s[–—]\s?", text, maxsplit=1)[0]
    text = _clean_wiki_title(text)
    text = _TRAILING_DURATION.sub("", text).strip()
    return text or None


_INFOBOX_TYPE = re.compile(r"\|\s*(?:type|vrsta)\s*=\s*([^\n|}]+)", re.IGNORECASE)
_INFOBOX_TYPE_MAP = (
    ("studio", "album"), ("studij", "album"),
    ("greatest", "compilation"), ("box", "compilation"),
    ("compilation", "compilation"), ("kompilac", "compilation"),
    ("remix", "compilation"), ("soundtrack", "compilation"),
    ("live", "live"), ("koncert", "live"),
    ("video", "video"),
)


_STUDIO_MARKER = re.compile(
    r"^(?:=+\s*|'''?\s*|;\s*)?(?:studio albums?|studijski albumi)\b",
    re.IGNORECASE,
)
_SECTION_MARKER = re.compile(r"^(?:==|'''|;)")
_LIST_LINK = re.compile(r"\[\[([^\]|#]+)(?:[^\]]*)?\]\]")


_ROW_HEADER = re.compile(r"!\s*scope\s*=\s*\"?row", re.IGNORECASE)
_ITALIC_TITLE = re.compile(r"''+\s*([^'{}\[\]<>]+?)\s*''+")


def _clean_album_title(title: str) -> str:
    # strip "Title (album)" / "Title (1975)"-style disambiguators
    return re.sub(r"\s*\((?:[^)]*album[^)]*|\d{4})\)$", "", title.strip())


def parse_studio_albums(wikitext: str) -> list[str] | None:
    """Titles under a 'Studio albums' marker up to the next section. Handles
    the three shapes Wikipedia uses: discography-article wikitables
    (! scope="row" | ''[[Title]]''), bullet lists with links, and bullet
    lists with bare italic titles."""
    rows: list[str] = []
    bullets: list[str] = []
    collecting = False
    for line in wikitext.splitlines():
        stripped = line.strip()
        if _STUDIO_MARKER.match(stripped):
            collecting = True
            continue
        if not collecting:
            continue
        if stripped.startswith("==") or (
            re.match(r"^(?:'''|;)", stripped)
            and not stripped.startswith(("|", "!", "{"))
        ):
            break
        if _ROW_HEADER.match(stripped):
            match = _LIST_LINK.search(stripped) or _ITALIC_TITLE.search(stripped)
            if match:
                rows.append(_clean_album_title(match.group(1)))
            continue
        if stripped.startswith("*"):
            match = _LIST_LINK.search(stripped)
            if match is None:
                match = re.match(r"\*+\s*''+\s*([^'(]+?)\s*''+", stripped)
            if match:
                bullets.append(_clean_album_title(match.group(1)))
    # table rows are unambiguous; bullet collections can drag in citation
    # links, so they only count when the section had no table
    albums = rows or bullets
    return list(dict.fromkeys(a for a in albums if a)) or None


def parse_infobox_type(wikitext: str) -> str | None:
    """Album type from the {{Infobox album}} 'type' (en) / 'vrsta' (hr)
    parameter, mapped onto our record_type values."""
    match = _INFOBOX_TYPE.search(wikitext)
    if not match:
        return None
    value = re.sub(r"[\[\]{}']", " ", match.group(1)).strip().lower()
    for key, mapped in _INFOBOX_TYPE_MAP:
        if key in value:
            return mapped
    if re.search(r"\bep\b", value):
        return "ep"
    return None


def parse_wikipedia_tracklist(wikitext: str) -> list[str] | None:
    """Track titles in article order, from {{Track listing}} templates or a
    numbered list under a track-listing heading. None when neither parses."""
    titles: list[str] = []
    for body in _tracklist_template_bodies(wikitext):
        titles.extend(_template_titles(body))
    if titles:
        return titles
    for match in _HEADING.finditer(wikitext):
        if not _TRACK_HEADING.match(_clean_wiki_title(match.group(2))):
            continue
        level = len(match.group(1))
        end = len(wikitext)
        for nxt in _HEADING.finditer(wikitext, match.end()):
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        for line in wikitext[match.end():end].splitlines():
            if line.startswith("#") and not line.startswith(("##", "#:", "#*")):
                title = _title_from_list_line(line)
                if title:
                    titles.append(title)
        if titles:
            return titles
    return None


# --- the year an artist appeared ----------------------------------------
# Wikidata carries no date for a good number of regional acts while their
# article states it plainly, in the infobox and again in the first sentence.

_INFOBOX_SOLO = re.compile(
    r"\|\s*background\s*=\s*[^\n]*\b(?:solo\w*|non_performing_personnel"
    r"|non_vocal_instrumentalist|classical_ensemble)", re.IGNORECASE)

_BIRTH_FIELDS = ("rođenje", "rodenje", "datum[_ ]rođenja", "rođen",
                 "born", "birth[_ ]?date", "birth[_ ]?year")
_START_FIELDS = ("djeluje[_ ]od", "djelatno[_ ]razdoblje",
                 "godine[_ ]d(?:je|e)lovanja",
                 "godine[_ ]aktivnosti",
                 "aktivn[ao][_ ]od", "osnutak", "osnovan[ai]?", "osnivanje",
                 "years[_ ]active", "formed", "origin[_ ]year", "active")

_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")

_MUSIC_WORDS = re.compile(
    r"\b(sastav|grupa|bend|glazben\w*|muzičk\w*|p(?:je|e)vač\w*|kantautor\w*|"
    r"reper\w*|skladatelj\w*|kompozitor\w*|klapa|zbor|band|musician|singer|"
    r"songwriter|rapper|composer|duo|hip[- ]hop|rock|pop|jazz)\b", re.IGNORECASE)

_PROSE_START = re.compile(
    r"(?:osnovan\w*|utemeljen\w*|nastao|nastala|okupio|okupili|"
    r"djeluje\s+od|aktivn\w*\s+(?:je\s+)?od|"
    r"formed|founded|established|active\s+since|began\s+in)"
    r"(?:[^.\n]|(?<=\d)\.){0,60}?\b(1[89]\d\d|20\d\d)\b", re.IGNORECASE)
_PROSE_BIRTH = re.compile(
    r"(?:rođen\w*|born)(?:[^.\n]|(?<=\d)\.){0,60}?\b(1[89]\d\d|20\d\d)\b", re.IGNORECASE)


def _field_year(wikitext: str, fields: tuple[str, ...]) -> int | None:
    """First plausible year in an infobox parameter. Values are written every
    way there is — '1996.–danas', '{{Birth date|1974|3|21}}', '[[1996.]]' —
    so the parameter is read to the end of its line and the first year in it
    is taken."""
    for field in fields:
        match = re.search(rf"\|\s*{field}\s*=\s*([^\n]*)", wikitext, re.IGNORECASE)
        if match is None:
            continue
        year = _year_in(match.group(1))
        if year is not None:
            return year
    return None


def _year_in(value: str) -> int | None:
    value = _REF_TAG.sub("", value)
    for found in _YEAR.finditer(value):
        return int(found.group(1))
    return None


def about_music(wikitext: str) -> bool:
    """Is this article about a musician at all — the gate on a same-named
    article found by search rather than by an identifier."""
    return _MUSIC_WORDS.search(wikitext[:1500]) is not None


def begin_year(wikitext: str, person: bool | None = None) -> int | None:
    """The year to file the artist under, in the same order Wikidata is read:
    a person by birth then by when they started working, a group by when it
    formed. Which of the two an article is about comes from the infobox when
    the caller does not already know."""
    body = _HTML_COMMENT.sub("", wikitext)
    if person is None:
        person = _INFOBOX_SOLO.search(body) is not None
    order = ((_BIRTH_FIELDS, _START_FIELDS) if person
             else (_START_FIELDS, _BIRTH_FIELDS))
    for fields in order:
        year = _field_year(body, fields)
        if year is not None:
            return year
    return None


def year_in_prose(text: str, person: bool | None = None) -> int | None:
    """The same year read out of the article's opening sentences, for the
    articles that state it in words and keep no infobox."""
    order = ((_PROSE_BIRTH, _PROSE_START) if person
             else (_PROSE_START, _PROSE_BIRTH))
    head = text[:1500]
    for pattern in order:
        match = pattern.search(head)
        if match is not None:
            return int(match.group(1))
    return None


# The line-up fields of {{Infobox musical artist}}, English and Croatian. Past
# members are asked for by name rather than swept up with the present ones: a
# band's page is who is in it, and a list that mixes the two says neither.
_MEMBER_FIELDS = {
    "current": ("current_members", "members", "sadašnji_članovi", "članovi"),
    "past": ("past_members", "bivši_članovi"),
}
# A line-up is written one per line, one per bullet, or piped inside a list
# template — `{{hlist|Richie Furay|Stephen Stills|…}}`. The pipe only becomes a
# separator once the wikilinks are resolved, because `[[Bill Ward (musician)|Bill
# Ward]]` carries one of its own.
_MEMBER_SPLIT = re.compile(r"<br\s*/?>|\n\s*\*+\s*|\n|\|")
_LIST_TEMPLATE = re.compile(
    r"\{\{\s*(?:plainlist|flatlist|hlist|ubl|unbulleted list|nowrap)\s*\|?",
    re.IGNORECASE)
# what a member played, which the infobox writes beside them and this is not
# asking about
_MEMBER_ASIDE = re.compile(r"<small>.*?</small>|<[^>]+>", re.IGNORECASE | re.DOTALL)


def _infobox_block(wikitext: str) -> str:
    """The first {{Infobox ...}} of an article, ending where its braces close.

    Reading fields out of the whole article is what let a line-up run off the
    end of the box and swallow the prose beneath it — The Lemonheads came back
    with a hundred and twenty-seven members, one of them `==History==`."""
    match = re.search(r"\{\{\s*infobox", wikitext, re.IGNORECASE)
    if match is None:
        return ""
    depth, i = 0, match.start()
    while i < len(wikitext) - 1:
        pair = wikitext[i:i + 2]
        if pair == "{{":
            depth += 1
            i += 2
            continue
        if pair == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[match.start():i]
            continue
        i += 1
    return wikitext[match.start():]


def parse_infobox_members(wikitext: str) -> dict[str, list[str]]:
    """Who a band's Wikipedia infobox says is in it, now and before.

    Wikidata carries a line-up for some bands and nothing for most; the article
    that describes them almost always has both lists, and it is the same
    infobox this module already reads the album type out of. Names come one per
    line separated by `<br />`, sometimes wrapped in a plainlist, and some are
    wikilinks — `[[Saša Antić]]` is the person, `Mladen Badovinac` beside it is
    the same kind of fact without an article behind it."""
    wikitext = _infobox_block(wikitext)
    found: dict[str, list[str]] = {}
    for kind, fields in _MEMBER_FIELDS.items():
        for field in fields:
            match = re.search(
                r"^\s*\|\s*" + re.escape(field)
                # `[ \t]*` and not `\s*`: an empty field is `= ` followed by the
                # newline, and letting that newline be eaten runs the capture on
                # into the next field — Black Sabbath's empty `current_members`
                # swallowed the `past_members` beneath it whole
                + r"\s*=[ \t]*(.+?)(?=^\s*\|\s*[\w_]+\s*=|^\s*\}\})",
                wikitext, re.MULTILINE | re.DOTALL | re.IGNORECASE)
            if match is None:
                continue
            raw = _REF_TAG.sub("", _HTML_COMMENT.sub("", match.group(1)))
            # the line break IS the separator, so it becomes one before the
            # tags around it are taken away
            raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
            raw = _MEMBER_ASIDE.sub("", raw)
            raw = _WIKILINK.sub(r"\1", raw)
            raw = _LIST_TEMPLATE.sub("", raw)
            pieces = _MEMBER_SPLIT.split(raw)
            # a comma is the separator of last resort, and only where the field
            # offered no other: `Sammy Davis, Jr.` is one man, and splitting
            # every list on commas would make him two
            if len([p for p in pieces if (p or "").strip()]) == 1 and "," in raw:
                pieces = raw.split(",")
            names = []
            for piece in pieces:
                name = _clean_wiki_title(piece or "")
                name = re.sub(r"\{\{|\}\}", "", name).strip(" \t|*\u2013-")
                # what is left of a template once its name is gone: a language
                # code, a stray parameter. A person's name is longer than that.
                if len(name) <= 3 and name.islower():
                    continue
                # an instrument list in brackets is what they played, not who
                name = re.sub(r"\s*\([^()]*\)\s*$", "", name)
                name = name.strip(" \t\u2013\u2014-,")
                # two people written on one line with no break between them
                # come back joined by the dash that separated the first from
                # their instrument. Losing a member is better than inventing
                # one, so a joined pair is dropped rather than stored.
                if re.search(r"\s[\u2013\u2014-]\s", name):
                    continue
                if name and len(name) <= 120 and not name.lower().startswith("see "):
                    names.append(name)
            if names:
                found[kind] = names
                break
    return found

