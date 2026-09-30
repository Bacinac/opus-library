"""Release search — through OPUS now, which drives Prowlarr.

The module keeps its name and its shape because that is all the pipeline ever
wanted from it: a query and a category in, normalized candidates out. What
changed is underneath — the library holds no Prowlarr URL or API key, and the search
that comes back is already protocol-normalized, so the candidate's channel is
simply the engine OPUS says can grab it."""

from opus import acquire
from opus.video.channels.base import Candidate

CATEGORY_MOVIES = 2000
CATEGORY_TV = 5000

# OPUS speaks in content types and maps them to the indexer's categories itself
_TYPES = {CATEGORY_MOVIES: "movie", CATEGORY_TV: "tv"}


async def search(config, query: str, category: int) -> list[Candidate]:
    releases = await acquire.search(config, query, _TYPES.get(category, "any"),
                                    ["prowlarr"])
    return [
        Candidate(
            # the engine OPUS named is the channel that can take it
            channel=r["grab_ref"]["engine"],
            title=r["title"],
            size=r.get("size") or 0,
            protocol=r["protocol"],
            seeders=r.get("seeders"),
            indexer=r.get("indexer", ""),
            guid=r.get("guid") or "",
            age_days=r.get("age_days"),
            subs_hint=r.get("subs_hint"),
            ref={"grab_ref": r["grab_ref"]},
        )
        for r in releases
    ]
