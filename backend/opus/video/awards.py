"""The award shelves. Winners are gathered into the library's own tables and
refreshed weekly: gathering one award asks Wikipedia, Wikidata and TMDB a hundred
questions, and a shelf has to open at once."""

import asyncio
import datetime
import logging

from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert

from opus.db import SessionLocal
from opus.models import AwardTitle, AwardWin
from opus.settings_store import current_runtime
from opus.video.metadata import tmdb
from opus.video.metadata.awards import AWARDS, Award, AwardsError, winners

log = logging.getLogger(__name__)

MAX_AGE = datetime.timedelta(days=7)
TMDB_AT_ONCE = 8
LOOP_IDLE = 24 * 3600

_gathering = asyncio.Lock()


Key = tuple[str, int]


async def _gather(config, award: Award) -> tuple[dict[Key, set[int]], dict[Key, dict], list[str]]:
    """(media type, tmdb id) → the years it won, its card, and what could not
    be placed. The award's own kind is preferred and the other taken when that
    is all the title is: the limited-series Emmy has gone to television films."""
    found = await winners(award)
    gate = asyncio.Semaphore(TMDB_AT_ONCE)
    order = (award.kind, "tv" if award.kind == "movie" else "movie")
    lost: list[str] = []

    async def placed(winner) -> Key | None:
        ids = winner.tmdb
        if not ids and winner.imdb_id:
            async with gate:
                ids = await tmdb.find_by_imdb(config, winner.imdb_id)
        return next(((m, ids[m]) for m in order if m in ids), None)

    years: dict[Key, set[int]] = {}
    for winner, key in zip(found, await asyncio.gather(*(placed(w) for w in found))):
        if key is None:
            lost.append(f"{winner.article} ({winner.year})")
        else:
            years.setdefault(key, set()).add(winner.year)

    async def card(key: Key) -> dict | None:
        async with gate:
            try:
                return await tmdb.card(config, *key)
            except tmdb.TmdbError as exc:
                # a Wikidata id TMDB has since merged away loses one title; any
                # other failure is TMDB being down, and the old shelf stands
                if exc.status == 404:
                    return None
                raise

    cards = {}
    for key, found_card in zip(years, await asyncio.gather(*(card(k) for k in years))):
        if found_card is None:
            lost.append(f"TMDB {key[0]} {key[1]} ({', '.join(map(str, sorted(years[key])))})")
        else:
            cards[key] = found_card
    return years, cards, lost


async def refresh(session, config, award: Award) -> None:
    years, cards, lost = await _gather(config, award)
    if not cards:
        raise AwardsError(f"{award.key}: not one winner could be placed on TMDB")
    if lost:
        log.warning("awards: %s could not place %d winner(s): %s", award.key, len(lost), "; ".join(lost))

    now = datetime.datetime.now(datetime.UTC)
    stmt = insert(AwardTitle).values([{
        "media_type": media_type, "tmdb_id": tmdb_id, "title": c["title"], "year": c["year"],
        "poster_url": c["poster_url"], "vote_average": c["vote_average"],
    } for (media_type, tmdb_id), c in cards.items()])
    rows = await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["media_type", "tmdb_id"],
            set_={k: stmt.excluded[k] for k in ("title", "year", "poster_url", "vote_average")})
        .returning(AwardTitle.media_type, AwardTitle.tmdb_id, AwardTitle.id))
    ids = {(media_type, tmdb_id): title_id for media_type, tmdb_id, title_id in rows.all()}

    await session.execute(delete(AwardWin).where(AwardWin.award == award.key))
    session.add_all(AwardWin(award=award.key, year=year, title_id=ids[key], refreshed_at=now)
                    for key in cards for year in years[key])
    await session.flush()
    await session.execute(delete(AwardTitle).where(
        ~exists().where(AwardWin.title_id == AwardTitle.id)))
    await session.commit()
    log.info("awards: %s holds %d titles", award.key, len(cards))


async def _refreshed_at(session, key: str) -> datetime.datetime | None:
    return await session.scalar(select(func.max(AwardWin.refreshed_at)).where(AwardWin.award == key))


async def awarded(session, config, kind: str) -> dict:
    """Everything a shelf's awards went to, each title once, with the years it
    won under each award — a wall to be filtered rather than one per award."""
    mine = [award for award in AWARDS if award.kind == kind]
    for award in mine:
        if await _refreshed_at(session, award.key) is None:
            async with _gathering:
                if await _refreshed_at(session, award.key) is None:
                    await refresh(session, config, award)

    body = {award.key: award.body for award in mine}
    rows = await session.execute(
        select(AwardTitle, AwardWin.award, AwardWin.year).join(AwardWin.title)
        .where(AwardWin.award.in_(body)))
    titles: dict[int, tuple[AwardTitle, dict[str, set[int]]]] = {}
    for title, key, year in rows.all():
        titles.setdefault(title.id, (title, {}))[1].setdefault(body[key], set()).add(year)

    def latest(entry) -> int:
        return max(max(years) for years in entry[1].values())

    return {
        "awards": list(dict.fromkeys(body.values())),
        "titles": [{
            "tmdb_id": t.tmdb_id, "media_type": t.media_type, "title": t.title, "year": t.year,
            "poster_url": t.poster_url, "vote_average": t.vote_average,
            "won": {b: sorted(years) for b, years in won.items()},
        } for t, won in sorted(titles.values(), key=lambda tw: (-latest(tw), tw[0].title))],
    }


async def awards_loop():
    await asyncio.sleep(120)
    while True:
        for award in AWARDS:
            try:
                async with SessionLocal() as session:
                    at = await _refreshed_at(session, award.key)
                    if at is not None and datetime.datetime.now(datetime.UTC) - at < MAX_AGE:
                        continue
                    config = await current_runtime()
                    async with _gathering:
                        await refresh(session, config, award)
            except Exception:
                log.exception("awards: refreshing %s failed", award.key)
        await asyncio.sleep(LOOP_IDLE)
