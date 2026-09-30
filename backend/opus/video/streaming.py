"""Where the titles found out in the world can be watched without fetching them:
the subscription services JustWatch says they are on, and which of those the
house pays for.

What was never looked up is looked up while the wall waits, but only for a
second; the rest lands in the table behind the answer. The loop keeps the award
walls warm, refreshes anything a day old and forgets what nobody has looked at
for a month."""

import asyncio
import datetime
import logging

from sqlalchemy import delete, exists, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from opus.db import SessionLocal
from opus.models import AwardTitle, Streaming
from opus.settings_store import current_runtime
from opus.video.metadata import tmdb

log = logging.getLogger(__name__)

MAX_AGE = datetime.timedelta(days=1)
FORGOTTEN = datetime.timedelta(days=30)
TMDB_AT_ONCE = 16
# A lookup TMDB has not served lately takes it half a second, so an actor's
# hundred titles seen for the first time held their wall for three.
WAIT_S = 1.0
LOOP_IDLE = 3600

Key = tuple[str, int]

_behind: set[asyncio.Task] = set()


def subscribed(config) -> set[int]:
    return {int(part) for part in config.get("streaming_subscriptions").split(",")
            if part.strip().isdigit()}


async def _fetch(config, keys: list[Key], arrived: dict[Key, list[dict]]) -> dict[Key, list[dict]]:
    gate = asyncio.Semaphore(TMDB_AT_ONCE)
    failed: list[tuple[Key, Exception]] = []

    async def one(key: Key) -> None:
        async with gate:
            try:
                arrived[key] = await tmdb.streaming(config, *key)
            except tmdb.TmdbError as error:
                failed.append((key, error))

    await asyncio.gather(*(one(key) for key in keys))
    if failed:
        (media_type, tmdb_id), error = failed[0]
        log.warning("streaming: %d of %d lookups failed, e.g. %s %s: %s",
                    len(failed), len(keys), media_type, tmdb_id, error)
    return arrived


async def _store(session, found: dict[Key, list[dict]], now: datetime.datetime) -> None:
    if not found:
        return
    stmt = insert(Streaming).values([
        {"media_type": media_type, "tmdb_id": tmdb_id, "services": services,
         "checked_at": now, "asked_at": now}
        for (media_type, tmdb_id), services in found.items()])
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["media_type", "tmdb_id"],
        set_={"services": stmt.excluded.services, "checked_at": stmt.excluded.checked_at}))


async def _look_up(config, keys: list[Key], arrived: dict[Key, list[dict]]) -> None:
    found = await _fetch(config, keys, arrived)
    async with SessionLocal() as session:
        await _store(session, found, datetime.datetime.now(datetime.UTC))
        await session.commit()


def _settled(task: asyncio.Task) -> None:
    _behind.discard(task)
    if not task.cancelled() and task.exception():
        log.error("streaming: looking titles up failed: %s", task.exception())


async def attach(session, config, cards: list[dict], kind: str | None = None) -> list[dict]:
    """Every card told where it streams, each service marked when the house pays
    for it. A card whose lookup is still on its way carries no `streaming` at
    all, which is not the same answer as an empty list."""
    def key(card: dict) -> Key | None:
        media_type = card.get("media_type") or kind
        if media_type not in ("movie", "tv") or not card.get("tmdb_id"):
            return None
        return media_type, card["tmdb_id"]

    keys = list(dict.fromkeys(k for k in map(key, cards) if k))
    if not keys:
        return cards
    rows = await session.execute(
        select(Streaming.media_type, Streaming.tmdb_id, Streaming.services)
        .where(tuple_(Streaming.media_type, Streaming.tmdb_id).in_(keys)))
    known: dict[Key, list[dict]] = {(m, t): services for m, t, services in rows.all()}
    await session.execute(
        update(Streaming).where(tuple_(Streaming.media_type, Streaming.tmdb_id).in_(list(known)))
        .values(asked_at=datetime.datetime.now(datetime.UTC)))
    await session.commit()

    if missing := [k for k in keys if k not in known]:
        arrived: dict[Key, list[dict]] = {}
        task = asyncio.create_task(_look_up(config, missing, arrived))
        _behind.add(task)
        task.add_done_callback(_settled)
        await asyncio.wait({task}, timeout=WAIT_S)
        known.update(arrived)

    ours = subscribed(config)
    for card in cards:
        services = known.get(key(card))
        if services is not None:
            card["streaming"] = [{**s, "ours": s["id"] in ours} for s in services]
    return cards


async def services(session, config) -> list[dict]:
    """The services the titles looked at so far are on, the most-carried first,
    and the ones the house pays for whether or not anything carries them now.
    TMDB's own list for the region is fifty names, half of them shops and
    Indian regional catalogues."""
    seen: dict[int, dict] = {}
    for carried in (await session.scalars(select(Streaming.services))).all():
        for found in carried:
            seen.setdefault(found["id"], {**found, "titles": 0})["titles"] += 1
    for missing in subscribed(config) - seen.keys():
        seen[missing] = {"id": missing, "name": str(missing), "logo": None, "titles": 0}
    return sorted(seen.values(), key=lambda s: (-s["titles"], s["name"]))


async def keep_up(session, config) -> int:
    now = datetime.datetime.now(datetime.UTC)
    award = exists().where(AwardTitle.media_type == Streaming.media_type,
                           AwardTitle.tmdb_id == Streaming.tmdb_id)
    await session.execute(delete(Streaming).where(Streaming.asked_at < now - FORGOTTEN, ~award))
    stale = await session.execute(
        select(Streaming.media_type, Streaming.tmdb_id).where(Streaming.checked_at < now - MAX_AGE))
    cold = await session.execute(
        select(AwardTitle.media_type, AwardTitle.tmdb_id).where(~exists().where(
            Streaming.media_type == AwardTitle.media_type,
            Streaming.tmdb_id == AwardTitle.tmdb_id)))
    keys = list(dict.fromkeys([*map(tuple, stale.all()), *map(tuple, cold.all())]))
    # the award walls take TMDB twenty seconds, and a transaction left open
    # across them holds a connection for nothing
    await session.commit()
    await _store(session, await _fetch(config, keys, {}), now)
    await session.commit()
    return len(keys)


async def streaming_loop():
    await asyncio.sleep(30)
    while True:
        try:
            async with SessionLocal() as session:
                config = await current_runtime()
                if refreshed := await keep_up(session, config):
                    log.info("streaming: refreshed %d titles", refreshed)
        except Exception:
            log.exception("streaming: keeping up failed")
        await asyncio.sleep(LOOP_IDLE)
