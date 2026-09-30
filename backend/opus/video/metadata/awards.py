"""Who won what. Each award's Wikipedia navbox lists every winner with the year
it won; each winner's Wikidata item says which TMDB title that is.

Wikidata's own "award received" was the obvious source and measured badly:
eleven of the seventy-odd Emmy drama winners, several of them pinned on the
showrunner rather than the show. The navboxes are kept by the people who follow
the ceremonies and carried the 78th Emmys the day after."""

import logging
import re
from dataclasses import dataclass

import httpx

from opus import wiki

log = logging.getLogger("opus.video.awards")

BATCH = 50

TMDB_PROPERTY = {"movie": "P4947", "tv": "P4983"}
IMDB_PROPERTY = "P345"


@dataclass(frozen=True)
class Award:
    key: str
    # what a reader filters by: the Oscar, not which of its categories
    body: str
    kind: str
    navbox: str


AWARDS = (
    Award("oscar_picture", "oscar", "movie", "AcademyAwardBestPicture"),
    Award("oscar_international", "oscar", "movie", "Academy Award Best Foreign Language Film"),
    Award("palme_dor", "palme_dor", "movie", "Palme d'Or"),
    Award("golden_lion", "golden_lion", "movie", "Golden Lion"),
    Award("golden_bear", "golden_bear", "movie", "Golden Bear"),
    Award("bafta_film", "bafta", "movie", "BAFTA Best Film"),
    Award("emmy_drama", "emmy", "tv", "Primetime Emmy Award for Outstanding Drama Series"),
    Award("emmy_comedy", "emmy", "tv", "Primetime Emmy Award for Outstanding Comedy Series"),
    Award("emmy_limited", "emmy", "tv", "EmmyAward Limited Series"),
    Award("globe_tv_drama", "golden_globe", "tv", "GoldenGlobeTVDrama"),
)


@dataclass(frozen=True)
class Winner:
    year: int
    article: str
    tmdb: dict[str, int]
    imdb_id: str | None


class AwardsError(Exception):
    pass


_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SMALL = re.compile(r"\{\{small\|.*?\}\}")
_WHEN = re.compile(r"\(([^()]*)\)\s*$")
_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")
# a winner is set in italics; the links inside {{small|…}} are its seasons
_TITLE = re.compile(r"''\[\[([^|\]#]+)(?:#[^|\]]*)?(?:\|[^\]]*)?\]\]''")


def entries(wikitext: str) -> list[tuple[int, str]]:
    """(year, article) for every winner on the navbox. A line with a title and
    no year is a navbox that changed shape, and is said out loud."""
    found = []
    for line in _COMMENT.sub("", wikitext).splitlines():
        if not line.startswith("*"):
            continue
        line = _SMALL.sub("", line)
        when = _WHEN.search(line)
        years = _YEAR.findall(when.group(1)) if when else []
        titles = _TITLE.findall(line[:when.start()] if when else line)
        if titles and not years:
            log.warning("awards: a winner without a year: %s", line)
            continue
        found.extend((int(years[-1]), title.strip()) for title in titles)
    return found


async def _navbox(client: httpx.AsyncClient, name: str) -> str:
    text = await wiki.wikitext(client, "en", f"Template:{name}")
    if text is None:
        raise AwardsError(f"navbox Template:{name} is gone")
    return text


async def _items(client: httpx.AsyncClient, articles: list[str]) -> dict[str, str]:
    """article → Wikidata QID, through whatever normalisation and redirects
    Wikipedia applies on the way."""
    items: dict[str, str] = {}
    for start in range(0, len(articles), BATCH):
        batch = articles[start:start + BATCH]
        data = await wiki.wikipedia(client, "en", action="query", titles="|".join(batch),
                                    prop="pageprops", ppprop="wikibase_item",
                                    redirects=1, formatversion=2)
        query = data.get("query", {})
        hops = {h["from"]: h["to"] for h in query.get("normalized", []) + query.get("redirects", [])}
        qid = {p["title"]: p["pageprops"]["wikibase_item"]
               for p in query.get("pages", []) if "wikibase_item" in p.get("pageprops", {})}
        for article in batch:
            title, seen = article, set()
            while title in hops and title not in seen:
                seen.add(title)
                title = hops[title]
            if title in qid:
                items[article] = qid[title]
    return items


async def _ids(client: httpx.AsyncClient, qids: list[str]) -> dict[str, tuple[dict[str, int], str | None]]:
    """QID → ({movie|tv: TMDB id}, IMDb title id). Several values of one
    property are a merge Wikidata has not finished; the lowest is taken so a
    refresh is stable."""
    tmdb: dict[str, dict[str, set[int]]] = {}
    imdb: dict[str, set[str]] = {}
    for start in range(0, len(qids), BATCH):
        batch = qids[start:start + BATCH]
        query = (
            "SELECT ?item ?movie ?tv ?imdb WHERE { VALUES ?item { %s } "
            "OPTIONAL { ?item wdt:%s ?movie } OPTIONAL { ?item wdt:%s ?tv } "
            "OPTIONAL { ?item wdt:%s ?imdb } }"
            % (" ".join(f"wd:{q}" for q in batch), TMDB_PROPERTY["movie"], TMDB_PROPERTY["tv"],
               IMDB_PROPERTY))
        for row in await wiki.sparql(client, query):
            qid = row["item"]["value"].rsplit("/", 1)[-1]
            for media_type in TMDB_PROPERTY:
                if (value := row.get(media_type, {}).get("value", "")).isdigit():
                    tmdb.setdefault(qid, {}).setdefault(media_type, set()).add(int(value))
            if (value := row.get("imdb", {}).get("value", "")).startswith("tt"):
                imdb.setdefault(qid, set()).add(value)
    return {q: ({m: min(v) for m, v in tmdb.get(q, {}).items()},
                min(imdb[q]) if q in imdb else None)
            for q in qids}


async def winners(award: Award) -> list[Winner]:
    async with wiki.client(timeout=60) as client:
        found = entries(await _navbox(client, award.navbox))
        if not found:
            raise AwardsError(f"navbox Template:{award.navbox} lists no winners")
        articles = sorted({article for _, article in found})
        items = await _items(client, articles)
        ids = await _ids(client, sorted(set(items.values())))
    return [Winner(year, article, *ids.get(items.get(article, ""), ({}, None)))
            for year, article in found]
