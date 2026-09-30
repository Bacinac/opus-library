"""Passes that come round on an interval, reckoned from the last one that finished.

The clock is in the database rather than in the sleep: a loop that slept its
whole interval from boot never ran at all on a day with more deploys than
intervals, and a loop that ran at boot would run on every deploy. A pass that
failed has not finished, so the restart that ships its fix runs it at once."""

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.dialects.postgresql import insert

from opus.db import SessionLocal
from opus.models import LoopPass

log = logging.getLogger("opus.schedule")


async def due_in(name: str, interval: datetime.timedelta) -> float:
    async with SessionLocal() as session:
        last = await session.get(LoopPass, name)
    if last is None:
        return 0.0
    left = last.finished_at + interval - datetime.datetime.now(datetime.UTC)
    return max(left.total_seconds(), 0.0)


async def finished(name: str) -> None:
    now = datetime.datetime.now(datetime.UTC)
    async with SessionLocal() as session:
        await session.execute(
            insert(LoopPass).values(name=name, finished_at=now)
            .on_conflict_do_update(index_elements=[LoopPass.name], set_={"finished_at": now}))
        await session.commit()


async def every(name: str, interval: datetime.timedelta,
                run: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            await asyncio.sleep(await due_in(name, interval))
        except Exception:
            log.exception("%s: when it last ran could not be read", name)
            await asyncio.sleep(interval.total_seconds())
        try:
            await run()
        except Exception:
            log.exception("%s pass failed", name)
            await asyncio.sleep(interval.total_seconds())
            continue
        try:
            await finished(name)
        except Exception:
            log.exception("%s: that it ran could not be kept", name)
            await asyncio.sleep(interval.total_seconds())
