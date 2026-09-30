"""Wikidata + Wikipedia client — the artist identity backbone. A Wikidata QID
links every external source (Deezer P2722, Spotify P1902, Discogs P1953,
MusicBrainz P434), the Wikipedia bio, the Commons portrait (P18) and group
membership (P463 member of / P527 has part)."""

import logging
import re
from urllib.parse import quote

from opus import wiki
from opus.music.metadata import wikitext as wt
from opus.music.textnorm import latin, norm, same_artist

log = logging.getLogger("opus.wikidata")

COMMONS_FILE_URL = "https://commons.wikimedia.org/wiki/Special:FilePath/{name}?width=1000"

EXTERNAL_ID_PROPS = {
    "P2722": "deezer",
    "P1902": "spotify",
    "P1953": "discogs",
    "P434": "musicbrainz",
}
PERSON_QID = "Q5"
CROATIA_QID = "Q224"

# dissolved states — when Wikidata lists both one of these and a modern
# successor for an artist's origin, the modern country is what a listener
# expects (SFR Yugoslavia -> Slovenia/Croatia/…)
DISSOLVED_STATES = {
    "Q83286",   # Socialist Federal Republic of Yugoslavia
    "Q36704",   # Yugoslavia (Kingdom)
    "Q37024",   # Serbia and Montenegro / FR Yugoslavia
    "Q15180",   # Soviet Union
    "Q33946",   # Czechoslovakia
    "Q16957",   # East Germany
    "Q713750",  # SR Croatia (within SFRJ)
    "Q1462",    # SR Slovenia
}


class WikidataError(Exception):
    pass


# plain-text extract section headings after which the bio stops being prose
_BIO_CUTOFF = re.compile(
    r"\n=+\s*(?:Diskografija|Discography|Izvori|References|Vanjske poveznice|"
    r"External links|Literatura|Bilješke|Notes|See also|Filmografija|"
    r"Filmography|Nagrade|Awards)\s*=+",
    re.IGNORECASE,
)


def _ranked_claims(entity: dict, prop: str) -> list[dict]:
    """Claim order is arbitrary — rank is Wikidata's own verdict on which
    value holds. Deprecated values are never read; when the item marks a
    preferred one, the normal-rank alternatives are noise."""
    claims = [c for c in entity.get("claims", {}).get(prop, [])
              if c.get("rank") != "deprecated"]
    return [c for c in claims if c.get("rank") == "preferred"] or claims


def claim_values(entity: dict, prop: str) -> list:
    out = []
    for claim in _ranked_claims(entity, prop):
        datavalue = claim.get("mainsnak", {}).get("datavalue")
        if datavalue is not None:
            out.append(datavalue["value"])
    return out


def first_claim(entity: dict, prop: str):
    values = claim_values(entity, prop)
    return values[0] if values else None


def claim_item_ids(entity: dict, prop: str) -> list[str]:
    return [v["id"] for v in claim_values(entity, prop) if isinstance(v, dict) and "id" in v]


def current_item_ids(entity: dict, prop: str) -> list[str]:
    """Item ids whose claim is still in force: a value carrying an end-time
    qualifier yields to one without. London's P17 opens with the Roman Empire
    and ends with the United Kingdom — a band formed there is British."""
    claims = _ranked_claims(entity, prop)
    live = [c for c in claims if "P582" not in c.get("qualifiers", {})]
    out = []
    for claim in (live or claims):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and "id" in value:
            out.append(value["id"])
    return out


def claim_year(entity: dict, prop: str) -> int | None:
    value = first_claim(entity, prop)
    if isinstance(value, dict) and value.get("time"):
        try:
            return int(value["time"][1:5])
        except ValueError:
            return None
    return None


def entity_label(entity: dict, preferred: tuple[str, ...] = ("hr", "en")) -> str | None:
    labels = entity.get("labels", {})
    for lang in preferred:
        if lang in labels:
            return labels[lang]["value"]
    return next((l["value"] for l in labels.values()), None)


# non-Latin script blocks (Arabic, Hebrew, Cyrillic, Greek, CJK, Hangul,
# Kana, Thai, Devanagari) — a name with any is not a Latin display name
_NON_LATIN = re.compile(
    "[\u0370-\u03ff\u0400-\u04ff\u0530-\u05ff\u0600-\u06ff\u0e00-\u0e7f"
    "\u0900-\u097f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]"
)


def has_non_latin(text: str) -> bool:
    """The name is (partly) in a non-Latin script — a bad Deezer display name
    like ABBA's 'ابا' that the Wikidata label should replace."""
    return bool(_NON_LATIN.search(text))


def strip_disambiguator(name: str) -> str:
    """Drop a trailing '(band)'/'(sastav)'/'(singer)'-style qualifier — both
    Wikipedia article titles AND some Wikidata labels carry it ('Buldožer
    (sastav)'), and it pollutes the display name and the folded match alike."""
    return re.sub(r"\s*\([^)]*\)$", "", name).strip()


def entity_display_name(entity: dict,
                        preferred: tuple[str, ...] = ("hr", "en")) -> str | None:
    """A presentable Latin-script name. Preferred labels first; then the
    matching Wikipedia article title (always in that wiki's language — some
    items, e.g. ABBA, carry no en/hr label at all and would otherwise fall to
    an Arabic label); then any Latin label; last, any label."""
    labels = entity.get("labels", {})
    for lang in preferred:
        if lang in labels:
            return strip_disambiguator(labels[lang]["value"])
    sitelinks = entity.get("sitelinks", {})
    for lang in preferred + ("en", "hr"):
        sitelink = sitelinks.get(f"{lang}wiki")
        if sitelink and sitelink.get("title"):
            return strip_disambiguator(sitelink["title"])
    latin = next((l["value"] for l in labels.values()
                  if not _NON_LATIN.search(l["value"])), None)
    chosen = latin or next((l["value"] for l in labels.values()), None)
    return strip_disambiguator(chosen) if chosen else None


def is_person(entity: dict) -> bool:
    return PERSON_QID in claim_item_ids(entity, "P31")


_MUSIC_GROUP_QIDS = {"Q215380", "Q2088357", "Q281643", "Q9212979"}
_MUSIC_ID_PROPS = ("P1953", "P2722", "P1902", "P434", "P1728")  # discogs/deezer/spotify/mb/allmusic
_MUSIC_OCCUPATION_QIDS = {
    "Q639669",   # musician
    "Q177220",   # singer
    "Q488205",   # singer-songwriter
    "Q753110",   # songwriter
    "Q36834",    # composer
    "Q2252262",  # rapper
}


def is_musical_artist(entity: dict) -> bool:
    """A band/duo/ensemble by P31, a music occupation by P106, or anything
    carrying a music-service id — locally-known acts (Mance) often carry only
    the occupation claim, no service ids at all."""
    if _MUSIC_GROUP_QIDS & set(claim_item_ids(entity, "P31")):
        return True
    if _MUSIC_OCCUPATION_QIDS & set(claim_item_ids(entity, "P106")):
        return True
    return any(first_claim(entity, prop) for prop in _MUSIC_ID_PROPS)


def commons_image_url(entity: dict) -> str | None:
    filename = first_claim(entity, "P18")
    if not filename:
        return None
    return COMMONS_FILE_URL.format(name=quote(str(filename)))


class WikidataClient:
    def __init__(self):
        self._client = wiki.client()

    async def close(self):
        await self._client.aclose()

    async def qid_by_deezer_id(self, deezer_id: int) -> str | None:
        """Exact reverse lookup via P2722. Returns None unless exactly one hit."""
        query = f'SELECT ?item WHERE {{ ?item wdt:P2722 "{deezer_id}" }} LIMIT 2'
        bindings = await wiki.sparql(self._client, query)
        if len(bindings) != 1:
            return None
        return bindings[0]["item"]["value"].rsplit("/", 1)[-1]

    async def search(self, text: str) -> list[dict]:
        """Name search across hr and en labels/aliases, for manual resolution.
        Each hit carries the text that actually matched: an alias hit asserts
        identity just like a label hit (Vojko V's hr alias 'Vojko Vrućina'),
        and the label alone would hide it from exact-name gates."""
        merged: dict[str, dict] = {}
        for language in ("hr", "en"):
            found = await wiki.wikidata(self._client, action="wbsearchentities",
                                        search=text, language=language,
                                        type="item", limit=10)
            for hit in found.get("search", []):
                merged.setdefault(hit["id"], {
                    "qid": hit["id"],
                    "label": hit.get("label", hit["id"]),
                    "description": hit.get("description", ""),
                    "match": (hit.get("match") or {}).get("text", ""),
                })
        return list(merged.values())

    async def albums_by_performer(self, artist_qid: str) -> list[dict]:
        """All albums performed by the artist, with canonical labels, original
        publication dates AND the P31 type — Wikipedia/Wikidata is the ground
        truth for what is a studio album vs a live album vs a compilation."""
        query = (
            "SELECT ?item ?itemLabel ?date ?type WHERE { "
            f"?item wdt:P175 wd:{artist_qid} . ?item wdt:P31 ?type . "
            "VALUES ?type { wd:Q482994 wd:Q209939 wd:Q222910 } "
            "OPTIONAL { ?item wdt:P577 ?date } "
            'SERVICE wikibase:label { bd:serviceParam wikibase:language "en,hr". } }'
        )
        type_map = {"Q482994": "album", "Q209939": "live", "Q222910": "compilation"}
        albums: dict[str, dict] = {}
        for binding in await wiki.sparql(self._client, query):
            qid = binding["item"]["value"].rsplit("/", 1)[-1]
            label = binding.get("itemLabel", {}).get("value")
            if not label or (label.startswith("Q") and label[1:].isdigit()):
                label = None
            entry = albums.setdefault(
                qid, {"qid": qid, "label": label, "date": None, "types": set()}
            )
            type_qid = (binding.get("type", {}).get("value") or "").rsplit("/", 1)[-1]
            if type_qid in type_map:
                entry["types"].add(type_map[type_qid])
            raw = binding.get("date", {}).get("value")
            if raw:
                date = raw[:10]
                # year-precision dates surface as Jan 1 — keep just the year
                if date.endswith("-01-01"):
                    date = date[:4]
                current = entry["date"]
                if (current is None or date[:4] < current[:4]
                        or (date[:4] == current[:4] and len(date) > len(current))):
                    entry["date"] = date
        out = []
        for entry in albums.values():
            types = entry.pop("types")
            # an item typed both album and live/compilation is the latter
            entry["record_type"] = ("live" if "live" in types
                                    else "compilation" if "compilation" in types
                                    else "album" if types else None)
            out.append(entry)
        return out

    async def get_entities(self, qids: list[str]) -> dict[str, dict]:
        if not qids:
            return {}
        entities: dict[str, dict] = {}
        # wbgetentities accepts at most 50 ids per call
        for i in range(0, len(qids), 50):
            data = await wiki.wikidata(self._client, action="wbgetentities",
                                       ids="|".join(qids[i:i + 50]),
                                       props="claims|labels|descriptions|sitelinks|aliases")
            if "error" in data:
                raise WikidataError(f"wbgetentities failed: {data['error']}")
            entities.update(data.get("entities", {}))
        return entities

    async def artist_studio_albums(self, qid: str) -> list[str] | None:
        """The artist's OWN Wikipedia studio-albums list — the definitive
        main-discography authority (P31 and infoboxes both mislabel box sets
        and video releases). Tries the P358 discography article first, then
        the artist article's Discography section. None when neither parses."""
        found = await wiki.wikidata(self._client, action="wbgetentities", ids=qid,
                                    props="claims|sitelinks")
        entity = found.get("entities", {}).get(qid) or {}
        pages: list[tuple[str, str]] = []
        disco_qid = first_claim(entity, "P358")
        if isinstance(disco_qid, dict):
            disco_qid = disco_qid.get("id")
        if disco_qid:
            disco = await wiki.wikidata(self._client, action="wbgetentities",
                                        ids=disco_qid, props="sitelinks")
            links = (disco.get("entities", {}).get(disco_qid) or {}).get("sitelinks", {})
            for lang in ("en", "hr"):
                if links.get(f"{lang}wiki"):
                    pages.append((lang, links[f"{lang}wiki"]["title"]))
        for lang in ("en", "hr"):
            sitelink = entity.get("sitelinks", {}).get(f"{lang}wiki")
            if sitelink:
                pages.append((lang, sitelink["title"]))
        for lang, title in pages:
            wikitext = await wiki.wikitext(self._client, lang, title) or ""
            albums = wt.parse_studio_albums(wikitext)
            if albums:
                return albums
        return None

    async def artist_image(self, qid: str) -> str | None:
        """Portrait for artists the streaming services barely know: Wikidata
        P18 (Commons) first, the Wikipedia article's lead image second."""
        found = await wiki.wikidata(self._client, action="wbgetentities", ids=qid,
                                    props="claims|sitelinks")
        entity = found.get("entities", {}).get(qid) or {}
        url = commons_image_url(entity)
        if url:
            return url
        sitelinks = entity.get("sitelinks", {})
        for lang in ("en", "hr"):
            sitelink = sitelinks.get(f"{lang}wiki")
            if not sitelink:
                continue
            found = await wiki.wikipedia(
                self._client, lang, action="query", titles=sitelink["title"],
                prop="pageimages", pithumbsize=1000,
                # default free-only picks an arbitrary body image (a member's
                # portrait) over the fair-use lead photo
                pilicense="any", redirects=1, formatversion=2)
            pages = found.get("query", {}).get("pages", [])
            thumb = (pages[0].get("thumbnail") or {}).get("source") if pages else None
            if thumb:
                return thumb
        return None

    async def wikipedia_album_info(self, qid: str) -> dict:
        """{'tracklist', 'record_type'} from the album's Wikipedia articles
        (hr preferred, en fallback) in one sitelink resolution. The infobox
        type is the reliable album/live/compilation/video authority — P31 is
        sloppily 'album' for box sets and video releases. Values are None
        when no article carries them."""
        found = await wiki.wikidata(self._client, action="wbgetentities", ids=qid,
                                    props="sitelinks|claims")
        entity = found.get("entities", {}).get(qid) or {}
        sitelinks = entity.get("sitelinks", {})
        # canonical albums usually carry the other services' ids as claims:
        # P2723 Deezer album, P2205 Spotify album, P1954 Discogs master
        external_ids = {
            source: value
            for source, prop in (("deezer", "P2723"), ("spotify", "P2205"),
                                 ("discogs", "P1954"))
            if (value := first_claim(entity, prop))
        }
        tracklist: list[str] | None = None
        record_type: str | None = None
        for lang in ("hr", "en"):
            sitelink = sitelinks.get(f"{lang}wiki")
            if not sitelink:
                continue
            wikitext = await wiki.wikitext(self._client, lang, sitelink["title"])
            if not wikitext:
                continue
            if tracklist is None:
                tracklist = wt.parse_wikipedia_tracklist(wikitext)
            if record_type is None:
                record_type = wt.parse_infobox_type(wikitext)
            if tracklist and record_type:
                break
        if tracklist is None and record_type is None:
            log.debug("no Wikipedia album info for %s", qid)
        return {"tracklist": tracklist, "record_type": record_type,
                "external_ids": external_ids}

    async def wikipedia_wikitext(self, lang: str, title: str) -> str:
        return await wiki.wikitext(self._client, lang, title) or ""

    async def wikipedia_titles_named(self, lang: str, name: str) -> list[str]:
        """Articles whose TITLE is the artist's name, disambiguator aside
        ('Gibonni', 'Rodriguez (sastav)'). Full-text search would return
        every article that merely mentions them, which asserts nothing."""
        found = await wiki.wikipedia(self._client, lang, action="query", list="search",
                                     srsearch=name, srlimit=10, formatversion=2)
        titles = []
        for hit in found.get("query", {}).get("search", []):
            title = hit.get("title") or ""
            bare = re.sub(r"\s*\([^()]*\)\s*$", "", title)
            if same_artist(latin(bare), latin(name)):
                titles.append(title)
        return titles

    async def wikipedia_album_title(self, lang: str, artist: str, album: str) -> str | None:
        """The article about a RECORD, found by name when the catalogue has no
        Wikidata entity for it.

        Full-text search would answer with every article that mentions the
        record — a discography page, the artist's own article, a chart list —
        so a hit is only taken when its title carries the album's name and the
        text of it names the artist. A disambiguation page names nobody and is
        dropped by the same rule.
        """
        found = await wiki.wikipedia(self._client, lang, action="query", list="search",
                                     srsearch=f'"{album}" {artist}', srlimit=6,
                                     formatversion=2)
        wanted = norm(latin(album))
        who = norm(latin(artist))
        for hit in found.get("query", {}).get("search", []):
            title = hit.get("title") or ""
            qualifier = re.search(r"\(([^()]*)\)\s*$", title)
            bare = re.sub(r"\s*\([^()]*\)\s*$", "", title)
            if norm(latin(bare)) != wanted:
                continue
            said = norm(latin(re.sub(r"<[^>]+>", "", hit.get("snippet") or "")))
            if who not in norm(latin(title)) + " " + said:
                continue
            # A record and a song off it share a name, and Wikipedia tells them
            # apart in the qualifier — "Waterloo (song)" is not the album. With
            # no qualifier the article itself has to say what it is.
            if qualifier:
                if "album" not in norm(latin(qualifier.group(1))):
                    continue
            elif "album" not in said:
                continue
            return title
        return None

    async def wikipedia_extract(self, lang: str, title: str) -> dict | None:
        """Article text for the bio. The lead alone is often a single hr-wiki
        sentence while the substance sits in Životopis/Biography sections —
        when the lead is thin, the full plain extract is fetched and cut
        before the list-type sections."""
        async def fetch(intro_only: bool) -> tuple[str, str] | None:
            params = {
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "redirects": 1,
                "titles": title,
            }
            if intro_only:
                params["exintro"] = 1
            found = await wiki.wikipedia(self._client, lang, **params)
            for page in found.get("query", {}).get("pages", {}).values():
                extract = (page.get("extract") or "").strip()
                if extract:
                    slug = quote(page.get("title", title).replace(" ", "_"))
                    return extract, f"https://{lang}.wikipedia.org/wiki/{slug}"
            return None

        result = await fetch(intro_only=True)
        if result is None:
            return None
        extract, url = result
        if len(extract) < 400:
            full = await fetch(intro_only=False)
            if full is not None:
                body = _BIO_CUTOFF.split(full[0], maxsplit=1)[0]
                body = re.sub(r"^=+ *(.+?) *=+$", "", body, flags=re.MULTILINE)
                body = re.sub(r"\n{3,}", "\n\n", body).strip()
                if len(body) > len(extract):
                    extract = body[:4000]
        return {"extract": extract, "url": url}
