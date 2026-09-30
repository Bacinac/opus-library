import httpx

from opus import http

SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

_pace = http.Throttle(0.5)


def client(timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"User-Agent": http.USER_AGENT},
                             follow_redirects=True)


async def _json(session: httpx.AsyncClient, url: str, params: dict) -> dict:
    resp = await http.get(session, url, params={**params, "format": "json"},
                          throttle=_pace)
    resp.raise_for_status()
    return resp.json()


async def sparql(session: httpx.AsyncClient, query: str) -> list[dict]:
    return (await _json(session, SPARQL_URL, {"query": query}))["results"]["bindings"]


async def wikidata(session: httpx.AsyncClient, **params) -> dict:
    return await _json(session, WIKIDATA_API, params)


async def wikipedia(session: httpx.AsyncClient, lang: str, **params) -> dict:
    return await _json(session, f"https://{lang}.wikipedia.org/w/api.php", params)


async def wikitext(session: httpx.AsyncClient, lang: str, title: str) -> str | None:
    """None when the wiki has no such page."""
    data = await wikipedia(session, lang, action="parse", page=title, prop="wikitext",
                           redirects=1, formatversion=2)
    if "parse" not in data:
        return None
    return data["parse"].get("wikitext") or ""
